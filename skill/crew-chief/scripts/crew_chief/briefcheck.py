"""Brief secret and sensitive-path filter.

``check_brief`` scans a brief before it is handed to an executor and
returns one finding per distinct match. A finding names the rule and a
masked excerpt: the first four characters of the matched secret plus
``…``, so the full secret never leaves this module. The only unmasked
excerpt is ``sensitive-path``, whose excerpt is the path entry itself —
a path is not a secret.
"""

from __future__ import annotations

import fnmatch
import re

MASK = "…"

#: Path entries that are always checked, besides the configured ones.
DEFAULT_SENSITIVE_PATHS: tuple[str, ...] = (".env", "cookie")

# Rule name -> pattern, in reporting order.
_SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"github_pat_\w{20,}")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack-token", re.compile(r"xox[bap]-[\w-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}")),
    (
        "key-value-secret",
        re.compile(
            r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}"
        ),
    ),
    (
        "env-block",
        re.compile(
            r"(?m)^[A-Z][A-Z0-9_]{2,}=\S+(?:\r?\n[A-Z][A-Z0-9_]{2,}=\S+){2,}"
        ),
    ),
    ("cookie-header", re.compile(r"(?im)^\s*(set-)?cookie:")),
)

# A path entry matches only after one of these, so `.env` matches `.env`
# and `config/.env` but not `environment`.
_PATH_BOUNDARY_CHARS = frozenset({" ", "\t", "\r", "\n", '"', "'", "`", "/", "("})


def _normalise(text: str) -> str:
    """Lowercase and turn backslashes into slashes."""
    return text.replace("\\", "/").casefold()


def _path_token_matches(haystack: str, token: str) -> bool:
    """True when ``token`` occurs in ``haystack`` at a path-token boundary."""
    start = 0
    while True:
        index = haystack.find(token, start)
        if index < 0:
            return False
        if index == 0 or haystack[index - 1] in _PATH_BOUNDARY_CHARS:
            return True
        start = index + 1


def _entry_token(entry: object) -> str:
    """A path entry as it is matched: normalised, without a trailing ``/``."""
    return _normalise(str(entry)).rstrip("/")


def path_is_sensitive(path: str, entries: list[str] | tuple[str, ...]) -> bool:
    """Whether the project-relative ``path`` is named by one of ``entries``.

    The same rule :func:`check_brief` applies to a brief: an entry matches at
    a path-token boundary (so ``referencia`` matches ``referencia/x.txt`` and
    ``docs/referencia`` but not ``dereferencia``), case-insensitively and
    with backslashes read as slashes. An entry holding ``*``, ``?`` or ``[`` is an
    :mod:`fnmatch` pattern instead, tried on the whole path and on every
    ``/``-separated tail of it.
    """
    haystack = _normalise(path).strip("/")
    for entry in entries:
        token = _entry_token(entry)
        if not token:
            continue
        if any(char in token for char in "*?["):
            parts = haystack.split("/")
            tails = ("/".join(parts[i:]) for i in range(len(parts)))
            if any(fnmatch.fnmatchcase(tail, token) for tail in tails):
                return True
        elif _path_token_matches(haystack, token):
            return True
    return False


def _words(text: str) -> list[str]:
    """Path-like words of ``text``: split on spaces, quotes, brackets, commas."""
    words = re.split(r"""[\s"'`()<>,;]+""", text)
    return [word.rstrip(".:") for word in words if word]


def check_brief(text: str, sensitive_paths: list[str]) -> list[str]:
    """Findings for a brief's secrets and sensitive paths.

    Returns an empty list when the brief is safe, otherwise one string per
    distinct match, ``"<rule name>: <masked excerpt>"``.
    """
    findings: list[str] = []
    seen: set[tuple[str, str]] = set()

    def add(rule: str, match: str, excerpt: str | None = None) -> None:
        if (rule, match) in seen:
            return
        seen.add((rule, match))
        findings.append(f"{rule}: {excerpt if excerpt is not None else match[:4] + MASK}")

    for rule, pattern in _SECRET_RULES:
        for found in pattern.finditer(text):
            add(rule, found.group(0))

    haystack = _normalise(text)
    for entry in (*DEFAULT_SENSITIVE_PATHS, *sensitive_paths):
        token = _entry_token(entry)
        if not token:
            continue
        if any(char in token for char in "*?["):
            # A wildcard entry names no literal text: test each path-like
            # word of the brief with the matcher the worktree uses.
            if any(path_is_sensitive(word, [token]) for word in _words(text)):
                add("sensitive-path", token, excerpt=token)
        elif _path_token_matches(haystack, token):
            add("sensitive-path", token, excerpt=token)

    return findings
