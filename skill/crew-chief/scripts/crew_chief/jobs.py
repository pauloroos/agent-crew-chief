"""Job summaries: one JSON line per finished run in ``jobs/<machine>/<YYYY-MM>.jsonl`` next to the ledger.

``ACC summary`` writes it before the crew chief removes ``../<repo>-acc-logs/``, and ``ACC sync`` carries
it. Only counts, names and links leave the logs: never a brief, a log line or a diff. The keys are the
ones the first launcher that wrote these files used (Portuguese), so old and new lines read the same.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import ledger

# A launcher's id (``$CREW_CHIEF_JOB_ID``): its start time in UTC and a random suffix.
LAUNCHER_ID = re.compile(r"^(\d{8}-\d{6})-[0-9a-f]{6}$")
# Provider and ACC error messages, tight enough not to count project code quoted in a log
# (rateLimit, setTimeout...).
ERRORS = {
    "cota": re.compile(r"insufficient_quota|quota[ _-]?(exceeded|exhausted|reached)|exceeded (your|the) "
                       r"(current )?quota|usage limit (reached|exceeded)|out of credits|RESOURCE_EXHAUSTED", re.I),
    "429": re.compile(r"too many requests|\b(error|erro|status code|http)\W{0,3}429\b"
                      r"|rate.?limit(ed)? (exceeded|reached|hit)", re.I),
    "timeout": re.compile(r"timed out after|operation timed out|request timed out|deadline exceeded|ETIMEDOUT"
                          r"|exit (code|status)[ :=]*124\b|\brc[ :=]+124\b", re.I),
    "escrita_fora_da_pasta": re.compile(r"outside_write|wrote outside (the|its) worktree", re.I),
}
# A line of code or test output (numbered read, diff, JS/TS/Python) is not an error.
CODE = re.compile(r"^\s*(\d+[:|]|[+-]\s|//|#)|expect\(|=>|\b(const|let|function|import|export|return|await|"
                  r"def|raise|throw)\b|\b(it|test|describe)\(")
VERDICT = re.compile(r"^VERDICT: (pass|fail)\s*$")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def folder(machine: str) -> Path:
    return ledger.ledger_path().parent / "jobs" / machine


def known_ids(machine: str) -> set[str]:
    ids = set()
    for path in folder(machine).glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                ids.add(json.loads(line)["id"])
            except (ValueError, KeyError, TypeError):
                continue
    return ids


def _clean(value):
    if isinstance(value, str):
        return ledger.redact(value)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    return value


def _at(text) -> datetime | None:
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def summarize(repo: str, logs: Path, job: str, rc: int, pr: str | None, now: datetime | None = None) -> dict:
    """The summary line of this run; reads ``logs`` (left untouched) and this machine's grades."""
    end = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    me = ledger.machine()
    files = [p for p in logs.iterdir() if p.is_file() and not p.is_symlink()] if logs.is_dir() else []
    launcher = os.environ.get("CREW_CHIEF_JOB_ID", "")
    started = LAUNCHER_ID.match(launcher)
    if started:
        start = datetime.strptime(started.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    elif files:
        # shortcut: the oldest file in the logs folder marks the start; a folder left by an older run starts earlier
        start = datetime.fromtimestamp(min(p.stat().st_mtime for p in files), timezone.utc).replace(microsecond=0)
    else:
        start = end
    # a logs folder left by an earlier run (Orca mode kept it until 0.13.1) must not lend its verdicts or errors
    files = [p for p in files if p.stat().st_mtime >= start.timestamp()]
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", job)[:60]
    job_id = re.sub(r"[^A-Za-z0-9._-]", "-", launcher)[:80] if launcher else f"{start:%Y%m%d-%H%M%S}-{slug}"

    errors = dict.fromkeys(ERRORS, 0)
    verdicts = {}
    for path in sorted(files):
        if ".log" in path.name:
            for line in ANSI.sub("", path.read_text(encoding="utf-8", errors="replace")).splitlines():
                if not CODE.search(line):
                    for kind, pattern in ERRORS.items():
                        errors[kind] += bool(pattern.search(line))
        elif ("scrutineer" in path.name and path.suffix == ".md"
              and not path.stem.endswith(("-prompt", "-spec"))):
            found = [m.group(1) for m in map(VERDICT.match, path.read_text(
                encoding="utf-8", errors="replace").splitlines()) if m]
            verdicts[path.stem] = found[-1] if found else "sem veredito"

    used = [os.environ["CREW_CHIEF_COORDINATOR"]] if os.environ.get("CREW_CHIEF_COORDINATOR") else []
    name = repo.rsplit("/", 1)[-1]  # a launcher may pass owner/repo; grades carry the bare name
    for row in ledger.read():
        at = _at(row.get("at"))
        if (row.get("project") in (repo, name) and at is not None and start <= at <= end
                and row.get("machine", me) == me):
            used.append(f"{row.get('agent')}:{row.get('model')}")
    return _clean({
        "id": job_id, "maquina": me, "repo": repo,
        "inicio": start.isoformat(), "fim": end.isoformat(),
        "minutos": round((end - start).total_seconds() / 60, 1), "rc": rc,
        "modelos_usados": list(dict.fromkeys(used)),
        "vereditos_fiscal": verdicts,
        "erros": errors,
        "pr_url": pr or None,
    })


def write(line: dict) -> bool:
    """Append ``line`` to its month file; False when this machine already has a line with its id."""
    if line["id"] in known_ids(line["maquina"]):
        return False
    ledger.append_line(folder(line["maquina"]) / f"{line['inicio'][:7]}.jsonl", line)
    return True
