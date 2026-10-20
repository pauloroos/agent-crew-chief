"""Tests for ``acc setup``: detection, questions, the managed block and the CLI.

Every test keeps the user's real home out of reach: :func:`setup.detect` gets a
``tmp_path`` as ``home`` and a fake ``env`` mapping, and the CLI tests point
``AGENT_CREW_CHIEF_GLOBAL_PREFS`` at ``tmp_path``. No login file or secret
value is ever read, printed or stored: only the *kind* of account and the
names of environment variables.
"""

from __future__ import annotations

import io
import json
import shutil
import tomllib
from dataclasses import replace
from pathlib import Path

from crew_chief import cli, setup
from crew_chief.cli import main
from crew_chief.config import SETUP_BEGIN, SETUP_END, Prefs, load_prefs
from crew_chief.discover import Executor

#: A value that must never leave a login file.
SECRET = "sk-live-this-value-must-never-be-read-back"


def _prefs(tools=None, **overrides) -> Prefs:
    values = dict(
        tools=[dict(tool) for tool in tools or []],
        models=[],
        strong_order=["codex", "claude"],
        explore_rate=0.1,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
    )
    values.update(overrides)
    return Prefs(**values)


def _executor(executor_id: str, kind: str) -> Executor:
    name, model = executor_id.split(":", 1)
    tool = {"name": name, "kind": kind, "bin": name}
    return Executor(
        id=executor_id, tool=tool, model=model, cost="paid", trains=False
    )


def _only_bins(monkeypatch, names: set[str]) -> None:
    """Restrict ``shutil.which`` to ``names`` so no real CLI is discovered."""
    real_which = shutil.which

    def which(cmd, *args, **kwargs):
        return real_which(cmd, *args, **kwargs) if cmd in names else None

    monkeypatch.setattr(shutil, "which", which)


def _write_prefs(repo: Path, text: str) -> None:
    path = repo / "docs" / "crew-chief" / "preferences.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_STUB_PREFS = """
[[tools]]
name = "stub"
kind = "generic"
bin = "stub"
models = ["m"]
run_cmd = ["stub", "{prompt}"]

[[models]]
match = "stub:*"
cost = "free"
"""


# --- detect ----------------------------------------------------------------


def test_detect_reads_account_kinds_without_values(tmp_path, fake_bin, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": SECRET, "id_token": SECRET},
                "last_refresh": "2026-10-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": SECRET}}), encoding="utf-8"
    )
    fake_bin("codex", "print('')\n")
    _only_bins(monkeypatch, {"codex"})
    prefs = _prefs(
        [{"name": "codex", "kind": "codex", "bin": "codex", "models": ["gpt-5"]}]
    )

    detected = setup.detect(
        prefs, {"OPENROUTER_API_KEY": SECRET, "OPENROUTER_KEY": "x"}, home
    )

    assert detected.accounts == {
        "codex": "account",
        "claude": "account",
        "openrouter_key": "OPENROUTER_API_KEY",
    }
    assert [executor.id for executor in detected.executors] == ["codex:gpt-5"]
    # Only kinds and names come back: no token, no key value, no file text.
    assert SECRET not in repr(detected)
    assert SECRET not in json.dumps(detected.accounts)

    # A codex login with an API key is a different kind, not a different value.
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": SECRET}), encoding="utf-8"
    )
    assert setup.detect(prefs, {}, home).accounts["codex"] == "api-key"

    # No login file at all, and no key in the environment.
    (home / ".codex" / "auth.json").unlink()
    nothing = setup.detect(prefs, {}, home)
    assert nothing.accounts["codex"] is None
    assert nothing.accounts["openrouter_key"] is None


def test_detect_ignores_unreadable_login_files(tmp_path, fake_bin, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("not json", encoding="utf-8")
    fake_bin("codex", "print('')\n")
    _only_bins(monkeypatch, {"codex"})
    prefs = _prefs(
        [{"name": "codex", "kind": "codex", "bin": "codex", "models": ["gpt-5"]}]
    )

    assert setup.detect(prefs, {}, home).accounts["codex"] is None


# --- questions -------------------------------------------------------------


def test_questions_only_ask_what_cannot_be_detected():
    claude = _executor("claude:sonnet", "claude-code")
    unknown = setup.Detected(
        executors=[claude],
        accounts={"codex": None, "claude": None, "openrouter_key": None},
    )

    questions = setup.questions(unknown, "en")
    assert [question.key for question in questions] == [
        "claude_subscription",
        "cheap_budget_usd_day",
        "allow_training",
    ]
    assert questions[0].text == "Do you pay for a Claude subscription (Pro/Max)? [y/N]"
    assert questions[1].default == "0"
    assert "cheap" in questions[1].text
    assert questions[2].default == "n"

    # The credentials file already tells us the Claude account exists.
    known = replace(
        unknown, accounts={"codex": None, "claude": "account", "openrouter_key": None}
    )
    assert [question.key for question in setup.questions(known, "en")] == [
        "cheap_budget_usd_day",
        "allow_training",
    ]

    # No Claude Code installed: nothing to ask about a subscription.
    absent = replace(known, executors=[], accounts={})
    assert [question.key for question in setup.questions(absent, "en")] == [
        "cheap_budget_usd_day",
        "allow_training",
    ]

    # Portuguese texts, one per line, with the default in brackets.
    pt = {question.key: question for question in setup.questions(unknown, "pt-BR")}
    assert pt["claude_subscription"].text == (
        "Você paga assinatura do Claude (Pro/Max)? [s/N]"
    )
    assert pt["cheap_budget_usd_day"].text == (
        "Você aceita gastar dinheiro com modelos baratos por uso quando os grátis "
        "acabarem? Quanto por dia, em dólares? [0]"
    )
    assert pt["allow_training"].text == (
        "Pode usar modelos grátis que treinam com o que você manda? [s/N]"
    )
    assert setup.detect_lang({"LANG": "pt_BR.UTF-8"}) == "pt"
    assert setup.detect_lang({"LC_ALL": "C", "LANGUAGE": "pt_BR:en"}) == "pt"
    assert setup.detect_lang({"LANG": "en_US.UTF-8"}) == "en"
    assert setup.detect_lang({}) == "en"


# --- the block -------------------------------------------------------------


def test_render_block_promotes_subscriptions_to_included():
    detected = setup.Detected(
        executors=[_executor("codex:gpt-5", "codex")],
        accounts={"codex": "account", "claude": None, "openrouter_key": None},
    )

    block = setup.render_block(
        detected, {"cheap_budget_usd_day": "2.5", "allow_training": "no"}
    )

    assert block.splitlines()[0] == SETUP_BEGIN
    assert block.splitlines()[-1] == SETUP_END
    data = tomllib.loads(block)
    assert data["cheap_budget_usd_day"] == 2.5
    assert data["forbid_training"] is True
    assert data["models"] == [{"match": "codex:*", "cost": "included"}]

    # A yes to the subscription question promotes Claude as well.
    paid_claude = replace(
        detected, accounts={"codex": "api-key", "claude": None, "openrouter_key": None}
    )
    promoted = tomllib.loads(
        setup.render_block(
            paid_claude, {"claude_subscription": "yes", "allow_training": "s"}
        )
    )
    assert promoted["forbid_training"] is False
    assert promoted["cheap_budget_usd_day"] == 0
    assert promoted["models"] == [{"match": "claude:*", "cost": "included"}]

    # Nothing deduced and defaults accepted: no promotion at all.
    bare = tomllib.loads(
        setup.render_block(setup.Detected(executors=[], accounts={}), {})
    )
    assert bare == {"cheap_budget_usd_day": 0, "forbid_training": True}


def test_render_block_never_writes_a_bad_budget():
    detected = setup.Detected(executors=[], accounts={})
    for raw in ("-3", "nan", "inf", "two", ""):
        data = tomllib.loads(
            setup.render_block(detected, {"cheap_budget_usd_day": raw})
        )
        assert data["cheap_budget_usd_day"] == 0


def test_write_block_appends_then_replaces_preserving_user_text_and_crlf(tmp_path):
    path = tmp_path / "preferences.toml"
    user = (
        "\ufeff# my own preferences\r\n"
        "forbid_training = false\r\n"
        "\r\n"
        "explore_rate = 0.2\r\n"
        'review_default = "claude:sonnet"\r\n'
    )
    path.write_bytes(user.encode("utf-8"))
    detected = setup.Detected(
        executors=[_executor("codex:gpt-5", "codex")],
        accounts={"codex": "account", "claude": None, "openrouter_key": None},
    )

    setup.write_block(
        path, setup.render_block(detected, {"cheap_budget_usd_day": "5"})
    )
    appended = path.read_bytes().decode("utf-8")

    # Everything the user wrote is still there, byte for byte, before the block.
    assert appended.startswith(user)
    assert appended.count(SETUP_BEGIN) == 1 and appended.count(SETUP_END) == 1
    # The file keeps the BOM and every newline is still CRLF.
    assert appended.startswith("\ufeff")
    assert "\n" not in appended.replace("\r\n", "")
    assert appended.index(SETUP_BEGIN) < appended.index(SETUP_END)

    setup.write_block(
        path, setup.render_block(detected, {"cheap_budget_usd_day": "9"})
    )
    replaced = path.read_bytes().decode("utf-8")

    # The old block is gone, nothing else moved, and there is still one block.
    assert replaced.startswith(appended[: appended.index(SETUP_BEGIN)])
    assert replaced.endswith(appended[appended.index(SETUP_END) + len(SETUP_END):])
    assert replaced.count(SETUP_BEGIN) == 1
    assert "cheap_budget_usd_day = 9" in replaced
    assert "cheap_budget_usd_day = 5" not in replaced
    assert "\r\n" in replaced and "\n" not in replaced.replace("\r\n", "")


def test_setup_block_round_trips_through_config(tmp_path):
    """The block the command writes is the block config.py reads back."""
    path = tmp_path / "preferences.toml"
    detected = setup.Detected(
        executors=[_executor("codex:gpt-5", "codex")],
        accounts={"codex": "account", "claude": "account", "openrouter_key": None},
    )
    block = setup.render_block(
        detected, {"cheap_budget_usd_day": "1.5", "allow_training": "no"}
    )

    setup.write_block(path, block)
    prefs = load_prefs(tmp_path, path)

    assert prefs.cheap_budget_usd_day == 1.5
    assert prefs.forbid_training is True
    assert prefs.setup_models == [
        {"match": "codex:*", "cost": "included"},
        {"match": "claude:*", "cost": "included"},
    ]
    assert setup.has_block(path) is True


def test_has_block_is_false_without_a_complete_block(tmp_path):
    path = tmp_path / "preferences.toml"
    assert setup.has_block(path) is False

    path.write_text("explore_rate = 0.3\n", encoding="utf-8")
    assert setup.has_block(path) is False

    path.write_text(f"{SETUP_BEGIN}\ncheap_budget_usd_day = 0\n", encoding="utf-8")
    assert setup.has_block(path) is False

    path.write_text(f"{SETUP_BEGIN}\ncheap_budget_usd_day = 0\n{SETUP_END}\n", "utf-8")
    assert setup.has_block(path) is True


# --- CLI -------------------------------------------------------------------


def _home_with(tmp_path, codex: str | None = None, claude: bool = False) -> Path:
    """A throwaway home holding fake login files, never the user's real one."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if codex is not None:
        (home / ".codex").mkdir(exist_ok=True)
        fields = (
            {"tokens": {"access_token": SECRET}}
            if codex == "account"
            else {"OPENAI_API_KEY": SECRET}
        )
        (home / ".codex" / "auth.json").write_text(
            json.dumps(fields), encoding="utf-8"
        )
    if claude:
        (home / ".claude").mkdir(exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": SECRET}}), encoding="utf-8"
        )
    return home


def _patch_detect(monkeypatch, tmp_path, executors, home=None):
    """Run the real ``detect`` over a fake discovery and a throwaway home."""
    monkeypatch.setattr(setup, "discover", lambda prefs: (list(executors), []))
    monkeypatch.setattr(setup, "home_dir", lambda: home or (tmp_path / "home"))


def test_setup_without_tty_and_without_yes_exits_2(
    git_repo, tmp_path, monkeypatch, capsys
):
    global_file = tmp_path / "preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))
    monkeypatch.setattr(cli, "_has_tty", lambda: False)

    assert main(["setup", "--project", str(git_repo)]) == 2

    captured = capsys.readouterr()
    assert captured.err.strip() == (
        "no terminal: run acc setup --yes or answer in a terminal"
    )
    assert captured.out == ""
    assert not global_file.exists()


def test_setup_yes_writes_defaults(git_repo, tmp_path, monkeypatch, capsys):
    global_file = tmp_path / "preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))
    _patch_detect(
        monkeypatch,
        tmp_path,
        [
            _executor("codex:gpt-5", "codex"),
            _executor("opencode:opencode/gpt-oss-free", "opencode"),
            _executor("opencode:openrouter/x", "opencode"),
        ],
        home=_home_with(tmp_path, codex="account"),
    )

    assert main(["setup", "--yes", "--project", str(git_repo)]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == (
        "your tiers: free 1 · included 1 · cheap 1 (budget US$ 0) · paid 0"
    )
    prefs = load_prefs(git_repo, global_file)
    assert prefs.cheap_budget_usd_day == 0.0
    assert prefs.forbid_training is True
    assert prefs.setup_models == [{"match": "codex:*", "cost": "included"}]


def test_setup_interactive_uses_the_answers(git_repo, tmp_path, monkeypatch, capsys):
    global_file = tmp_path / "preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))
    _patch_detect(
        monkeypatch, tmp_path, [_executor("claude:sonnet", "claude-code")]
    )
    monkeypatch.setattr(cli, "_has_tty", lambda: True)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("y\n3.5\ns\n"))

    assert main(["setup", "--project", str(git_repo), "--lang", "pt"]) == 0

    captured = capsys.readouterr()
    assert "Você paga assinatura do Claude (Pro/Max)? [s/N]" in captured.err
    assert "Quanto por dia, em dólares? [0]" in captured.err
    assert "Pode usar modelos grátis que treinam com o que você manda? [s/N]" in (
        captured.err
    )
    assert captured.out.strip() == (
        "seus degraus: grátis 0 · incluído 1 · barato 0 (teto US$ 3.5) · caro 0"
    )
    prefs = load_prefs(git_repo, global_file)
    assert prefs.cheap_budget_usd_day == 3.5
    assert prefs.forbid_training is False
    assert prefs.setup_models == [{"match": "claude:*", "cost": "included"}]


def test_setup_tiers_are_counted_with_the_new_block(
    git_repo, tmp_path, monkeypatch, capsys
):
    """Counts already apply the promotion the block is about to add."""
    global_file = tmp_path / "preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))
    _patch_detect(
        monkeypatch,
        tmp_path,
        [
            _executor("codex:gpt-5", "codex"),
            _executor("claude:sonnet", "claude-code"),
            _executor("opencode:opencode/gpt-oss-free", "opencode"),
            _executor("opencode:openrouter/x", "opencode"),
            _executor("opencode:ghostcli/x", "opencode"),
        ],
        home=_home_with(tmp_path, codex="account"),
    )

    assert main(["setup", "--yes", "--project", str(git_repo)]) == 0

    out = capsys.readouterr().out.strip()
    assert out == (
        "your tiers: free 1 · included 1 · cheap 1 (budget US$ 0) · paid 2"
    )


def test_setup_json_payload(git_repo, tmp_path, monkeypatch, capsys):
    global_file = tmp_path / "preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))
    _patch_detect(monkeypatch, tmp_path, [_executor("codex:gpt-5", "codex")])

    assert (
        main(["setup", "--yes", "--lang", "en", "--json", "--project", str(git_repo)])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["lang"] == "en"
    assert payload["tiers"] == {"free": 0, "included": 0, "cheap": 0, "paid": 1}
    assert payload["cheap_budget_usd_day"] == 0.0
    assert payload["forbid_training"] is True
    assert payload["preferences"] == str(global_file)
    assert payload["accounts"]["codex"] is None


def test_discover_suggests_setup(git_repo, tmp_path, fake_bin, monkeypatch, capsys):
    repo = git_repo
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    _write_prefs(repo, _STUB_PREFS)

    assert main(["discover", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "run setup to configure cost tiers" in lines
    # The hint is a line of its own, before the closing work-types lines.
    assert lines.index("run setup to configure cost tiers") < next(
        i for i, line in enumerate(lines) if line.startswith("work types:")
    )
    assert main(["discover", "--project", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["setup_hint"] == (
        "run setup to configure cost tiers"
    )

    global_file = tmp_path / "preferences.toml"
    setup.write_block(
        global_file,
        setup.render_block(setup.Detected(executors=[], accounts={}), {}),
    )
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))

    assert main(["discover", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "run setup to configure cost tiers" not in out
    # The suggestion never displaces the closing work-types lines.
    assert out.rstrip().splitlines()[-1].startswith("work types: stub:")

    assert main(["discover", "--project", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["setup_hint"] is None
