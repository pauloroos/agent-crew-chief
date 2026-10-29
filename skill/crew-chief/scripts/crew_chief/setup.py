"""``acc setup``: detect the machine, ask the rest, write the managed block.

The command reads the machine once and writes one managed block into the
global preferences file, between the ``acc setup`` markers, leaving everything
the user wrote outside it byte for byte. It never prints or stores the *value*
of a key or a token: it reports only the kind of login (``"account"`` or
``"api-key"``) and the *names* of the environment variables it found.

Every function that touches the filesystem takes its ``home`` as an argument,
so tests use a throwaway directory and never read the user's real ``~/.codex``
or ``~/.claude``. The question loop takes its streams as arguments for the
same reason.
"""

from __future__ import annotations

import dataclasses
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TextIO

from .config import SETUP_BEGIN, SETUP_END, Prefs, classify, default_global_path
from .discover import discover
from .records import write_atomic
from .tiers import TIER_ORDER
from .tools import Executor

#: Provider key variables whose *presence* is reported (never their value).
PROVIDER_KEYS = ("OPENROUTER_API_KEY",)

#: Environment variables naming the system language, most specific first.
LANG_ENV = ("LC_ALL", "LANG", "LANGUAGE")

#: Entities promoted to ``included`` when their login says the plan is paid
#: for: the account key in :attr:`Detected.accounts` and the rule to add.
PROMOTIONS = (("codex", "codex:*"), ("claude", "claude:*"))

#: One ``(sentence, default in brackets, default)`` per question, per language.
_QUESTIONS_PT = {
    "claude_subscription": (
        "Você paga assinatura do Claude (Pro/Max)?",
        "s/N",
        "n",
    ),
    "cheap_budget_usd_day": (
        "Você aceita gastar dinheiro com modelos baratos por uso quando os "
        "grátis acabarem? Quanto por dia, em dólares?",
        "0",
        "0",
    ),
    "allow_training": (
        "Pode usar modelos grátis que treinam com o que você manda?",
        "s/N",
        "n",
    ),
}
_QUESTIONS_EN = {
    "claude_subscription": (
        "Do you pay for a Claude subscription (Pro/Max)?",
        "y/N",
        "n",
    ),
    "cheap_budget_usd_day": (
        "Are you willing to spend money on cheap pay-per-use models when the "
        "free ones run out? How much per day, in dollars?",
        "0",
        "0",
    ),
    "allow_training": (
        "May we use free models that train on what you send?",
        "y/N",
        "n",
    ),
}


@dataclass
class Detected:
    """What one detection pass learned about this machine.

    ``accounts`` holds only a kind per entity, never a credential:
    ``"codex"`` is ``"account"``/``"api-key"``/``None`` from the fields of
    ``~/.codex/auth.json``, ``"claude"`` is ``"account"``/``None`` from the
    existence of ``~/.claude/.credentials.json`` and ``"openrouter_key"`` is
    the *name* of the environment variable that is set, or ``None``.
    """

    executors: list[Executor]
    accounts: dict[str, str | None]


@dataclass
class Question:
    """One question of ``acc setup``: its key, its text and its default."""

    key: str
    text: str
    default: str


# --- detection -------------------------------------------------------------


def _codex_login(home: Path) -> str | None:
    """``"account"`` or ``"api-key"`` from the *fields* of the codex login.

    A ``tokens`` object means the ChatGPT account; a bare ``OPENAI_API_KEY``
    means an API key. Values are never read out: only which field is there.
    """
    try:
        data = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if isinstance(tokens, dict) and tokens:
        return "account"
    if data.get("OPENAI_API_KEY"):
        return "api-key"
    return None


def detect(prefs: Prefs, env: Mapping[str, str], home: Path) -> Detected:
    """Executors present now, plus the kind of each login and key name.

    Never returns a credential: a login file is read for the *presence* of
    its fields and an environment variable for its name alone.
    """
    home = Path(home)
    executors, _notes = discover(prefs)
    credentials = home / ".claude" / ".credentials.json"
    return Detected(
        executors=list(executors),
        accounts={
            "codex": _codex_login(home),
            "claude": "account" if credentials.is_file() else None,
            "openrouter_key": next(
                (name for name in PROVIDER_KEYS if env.get(name)), None
            ),
        },
    )


def home_dir() -> Path:
    """The home directory :func:`detect` reads logins from (tests patch this)."""
    return Path.home()


def detect_lang(env: Mapping[str, str]) -> str:
    """``"pt"`` when the system language is Portuguese, else ``"en"``."""
    for name in LANG_ENV:
        first = str(env.get(name, "")).split(":")[0].strip()
        if first.lower().startswith("pt"):
            return "pt"
    return "en"


# --- questions -------------------------------------------------------------


def _claude_installed(detected: Detected) -> bool:
    """Whether a ``claude-code`` executor was listed."""
    return any(
        executor.tool.get("kind") == "claude-code" for executor in detected.executors
    )


def questions(detected: Detected, lang: str) -> list[Question]:
    """The questions only detection could not answer, in ``lang``.

    The Claude subscription is asked only when Claude Code is installed and no
    credentials file was found; the budget and the training question are
    always asked, each with its default in brackets.
    """
    texts = _QUESTIONS_PT if lang.startswith("pt") else _QUESTIONS_EN
    keys = []
    if _claude_installed(detected) and detected.accounts.get("claude") is None:
        keys.append("claude_subscription")
    keys += ["cheap_budget_usd_day", "allow_training"]
    return [
        Question(key, f"{texts[key][0]} [{texts[key][1]}]", texts[key][2])
        for key in keys
    ]


def is_yes(answer: str) -> bool:
    """``s``/``sim``/``y``/``yes`` (any case) mean yes; everything else no."""
    return answer.strip().lower() in {"s", "sim", "y", "yes"}


def budget_value(answer: str | None) -> float:
    """The daily budget as a finite, non-negative number; anything else 0."""
    try:
        value = float(answer)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def _budget_text(answer: str | None) -> str:
    """``budget_value`` as TOML text (``0`` keeps its plain shape)."""
    return f"{budget_value(answer):g}"


def ask(questions: list[Question], stdin: TextIO, stdout: TextIO) -> dict[str, str]:
    """Ask one question at a time, taking the default on an empty answer.

    An EOF also takes the default. Yes/no answers come back as ``"yes"`` or
    ``"no"`` and the budget as its clean number, so :func:`render_block` never
    has to guess what the user typed.
    """
    answers: dict[str, str] = {}
    for question in questions:
        stdout.write(f"{question.text} ")
        stdout.flush()
        line = stdin.readline()
        answers[question.key] = _sanitize(question, line.strip() if line else "")
    return answers


def _sanitize(question: Question, answer: str) -> str:
    if not answer:
        return question.default
    if question.key == "cheap_budget_usd_day":
        return _budget_text(answer)
    return "yes" if is_yes(answer) else "no"


# --- the managed block -----------------------------------------------------


def promoted_rules(detected: Detected, answers: dict[str, str]) -> list[dict]:
    """The ``[[models]]`` rules promoting the paid-for subscriptions.

    Codex is promoted when its login is the ChatGPT account and Claude when
    the credentials file was found or the user said they pay for it.
    """
    rules: list[dict] = []
    for entity, match in PROMOTIONS:
        if entity == "claude":
            included = detected.accounts.get(entity) == "account" or is_yes(
                answers.get("claude_subscription", "n")
            )
        else:
            included = detected.accounts.get(entity) == "account"
        if included:
            rules.append({"match": match, "cost": "included"})
    return rules


def render_block(detected: Detected, answers: dict[str, str]) -> str:
    """The managed TOML block for ``answers``, markers included."""
    forbid_training = not is_yes(answers.get("allow_training", "n"))
    lines = [
        SETUP_BEGIN,
        "# Written by `acc setup`; run it again to change the answers.",
        f"cheap_budget_usd_day = {_budget_text(answers.get('cheap_budget_usd_day'))}",
        f"forbid_training = {'true' if forbid_training else 'false'}",
    ]
    for rule in promoted_rules(detected, answers):
        lines += [
            "",
            "[[models]]",
            f'match = "{rule["match"]}"',
            f'cost = "{rule["cost"]}"',
        ]
    lines.append(SETUP_END)
    return "\n".join(lines) + "\n"


def block_values(block: str) -> dict:
    """The block parsed on its own, exactly as ``config`` parses it."""
    return tomllib.loads(block)


def _block_span(text: str) -> tuple[int, int] | None:
    """Line indexes of the first complete managed block in ``text``."""
    lines = text.splitlines(keepends=True)
    begin = next(
        (i for i, line in enumerate(lines) if line.strip() == SETUP_BEGIN), None
    )
    if begin is None:
        return None
    end = next(
        (i for i in range(begin + 1, len(lines)) if lines[i].strip() == SETUP_END),
        None,
    )
    if end is None:
        return None
    return begin, end


def has_block(path: Path) -> bool:
    """Whether ``path`` holds a complete managed block (no file: no)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return _block_span(text) is not None


def setup_hint() -> str | None:
    """The suggestion ``discover`` adds while the global file has no block."""
    if has_block(default_global_path()):
        return None
    return "run setup to configure cost tiers"


def _normalized_block(block: str, newline: str) -> str:
    """``block`` with the file's newline style and exactly one trailing one."""
    text = block.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return text.replace("\n", newline) + newline


def write_block(path: Path, block: str) -> None:
    """Replace the managed block in ``path``, or append it at the end.

    Everything outside the markers stays byte for byte, BOM and CRLF
    included. The write goes through :func:`records.write_atomic`.
    """
    path = Path(path)
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        raise ValueError(f"{path}: cannot read preferences file: {exc}") from exc

    newline = "\r\n" if "\r\n" in text else "\n"
    managed = _normalized_block(block, newline)
    span = _block_span(text)
    if span is None:
        head = text
        if head and not head.endswith(("\n", "\r")):
            head += newline
        if head and not head.endswith(newline * 2):
            head += newline
        written = head + managed
    else:
        begin, end = span
        lines = text.splitlines(keepends=True)
        written = "".join([*lines[:begin], managed, *lines[end + 1 :]])
    write_atomic(path, written)


# --- the summary -----------------------------------------------------------


def tier_counts(prefs: Prefs, detected: Detected, block: str) -> dict[str, int]:
    """Executors per tier, with the rules of ``block`` already applied.

    The preferences are resolved as ``load_prefs`` would resolve them once the
    block is written, so a promotion the user is about to accept is counted.
    """
    values = block_values(block)
    effective = dataclasses.replace(
        prefs,
        setup_models=list(values.get("models", [])),
        forbid_training=bool(values.get("forbid_training", prefs.forbid_training)),
        cheap_budget_usd_day=budget_value(values.get("cheap_budget_usd_day")),
    )
    counts = {tier: 0 for tier in TIER_ORDER}
    for executor in detected.executors:
        cost, _trains = classify(
            executor.id, effective, executor.tool.get("cost_default")
        )
        if cost in counts:
            counts[cost] += 1
    return counts


#: Tier names of the summary, in :data:`TIER_ORDER`.
_SUMMARY_TIERS = {
    "pt": ("grátis", "incluído", "barato", "caro"),
    "en": TIER_ORDER,
}


def summary_text(counts: dict[str, int], budget: str, lang: str) -> str:
    """``your tiers: free 1 · included 0 · cheap 0 (budget US$ 0) · paid 2``."""
    if lang.startswith("pt"):
        head, ceiling, names = "seus degraus", "teto", _SUMMARY_TIERS["pt"]
    else:
        head, ceiling, names = "your tiers", "budget", _SUMMARY_TIERS["en"]
    numbers = [counts.get(tier, 0) for tier in TIER_ORDER]
    return (
        f"{head}: {names[0]} {numbers[0]} · {names[1]} {numbers[1]} · "
        f"{names[2]} {numbers[2]} ({ceiling} US$ {budget}) · "
        f"{names[3]} {numbers[3]}"
    )
