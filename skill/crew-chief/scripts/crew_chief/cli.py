"""``crew-chief.py``: record a graded job, show the scores, pick a model, import 0.6 grades."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, ledger, models, score


def _project_name() -> str:
    """The repository's name, the same from any of its worktrees."""
    root = models.repo_root()
    return root.name if root is not None else Path.cwd().name


def _score_value(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("score goes from 0 to 100")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crew-chief.py", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record", help="write the grade of one finished job")
    record.add_argument("--kind", required=True, choices=ledger.KINDS)
    record.add_argument("--agent", required=True, help="the Orca agent, e.g. codex, claude, opencode")
    record.add_argument("--model", required=True)
    record.add_argument("--effort")
    record.add_argument("--cost", choices=ledger.COSTS)
    record.add_argument("--outcome", required=True, choices=ledger.OUTCOMES)
    record.add_argument("--score", required=True, type=_score_value, help="0 to 100")
    record.add_argument("--minutes", type=float)
    record.add_argument("--lane")
    record.add_argument("--job", help="the job title")
    record.add_argument("--note", help="one line on why this score")
    record.add_argument("--project", help="default: this repository's name")

    show = commands.add_parser("score", help="the scoreboard")
    show.add_argument("--kind", choices=ledger.KINDS)
    show.add_argument("--project")
    show.add_argument("--json", action="store_true")

    listed = commands.add_parser("models", help="the declared models and their scores")
    listed.add_argument("--json", action="store_true")

    pick = commands.add_parser("pick", help="the model to use for a kind of work")
    pick.add_argument("--kind", required=True, choices=ledger.KINDS)
    pick.add_argument("--max-cost", choices=ledger.COSTS)
    pick.add_argument("--json", action="store_true")

    old = commands.add_parser("import", help="copy the grades of an ACC 0.6 project")
    old.add_argument("project", type=Path)
    return parser


def _match(rows: list[dict], entry: dict, kind: str | None = None) -> list[dict]:
    """The ledger rows that grade this declared model (same agent+model, same effort when set)."""
    return [
        row for row in rows
        if (kind is None or row["kind"] == kind)
        and row.get("agent") == entry["agent"]
        and row.get("model") == entry["model"]
        and (entry.get("effort") is None or row.get("effort") == entry.get("effort"))
    ]


def _untried(kind: str, entry: dict) -> dict:
    """A scoreboard row for a declared model with no grade yet."""
    return {
        "kind": kind, "executor": score.executor(entry), "agent": entry["agent"],
        "model": entry["model"], "effort": entry.get("effort"), "cost": entry["cost"],
        "score": None, "n": 0, "pass_rate": None, "minutes": None, "last": "",
    }


def main(argv: list[str] | None = None) -> int:
    # `python -I` ignores PYTHONUTF8: print UTF-8 so accents stay whole.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)

    if args.command in ("models", "pick", "score"):
        try:
            declared, files = models.load()
        except models.ModelsError as exc:
            print(exc, file=sys.stderr)
            return 2

    if args.command == "record":
        entry = ledger.append({
            "project": args.project or _project_name(),
            "kind": args.kind,
            "agent": args.agent,
            "model": args.model,
            "effort": args.effort,
            "cost": args.cost,
            "outcome": args.outcome,
            "score": args.score,
            "minutes": args.minutes,
            "lane": args.lane,
            "job": args.job,
            "note": args.note,
        })
        print(f"recorded {score.executor(entry)} {args.kind} {args.outcome} {args.score}")
        return 0

    if args.command == "score":
        rows = score.table(ledger.read(), args.kind, args.project)
        if not declared:
            print(json.dumps(rows, indent=2) if args.json else score.render(rows))
            return 0
        if args.kind:
            rows = [*rows, *(_untried(args.kind, m) for m in declared
                             if (m.get("kinds") is None or args.kind in m["kinds"])
                             and not _match(rows, m))]
            print(json.dumps(rows, indent=2) if args.json else score.render(rows))
            return 0
        missing = [m for m in declared if not _match(rows, m)]
        if not missing:
            print(json.dumps(rows, indent=2) if args.json else score.render(rows))
            return 0
        if args.json:
            rows = [*rows, *(_untried(kind, m) for m in missing
                             for kind in (m["kinds"] or ledger.KINDS))]
            print(json.dumps(rows, indent=2))
            return 0
        lines = [score.render(rows), "", "## not tried yet", ""]
        for m in missing:
            kinds = ", ".join(m["kinds"]) if m.get("kinds") else "all"
            line = f"- {score.executor(m)} (cost {m['cost']}, kinds {kinds})"
            lines.append(line + f": {m['note']}" if m.get("note") else line)
        print("\n".join(lines))
        return 0

    if args.command == "models":
        if not files:
            print("No models.toml found: ACC pick uses the scoreboard only.")
            project = models.project_path()
            print(f"looked for: {models.global_path()}"
                  + (f" and {project}" if project is not None
                     else " (not in a git repository: no project file)"))
            return 0
        rows = score.table(ledger.read())
        if args.json:
            print(json.dumps({
                "files": [str(p) for p in files],
                "models": [{
                    "agent": m["agent"], "model": m["model"], "effort": m.get("effort"),
                    "cost": m["cost"], "kinds": m.get("kinds"), "note": m.get("note"),
                    "source": m["source"],
                    "scores": [{"kind": r["kind"], "score": r["score"], "n": r["n"]}
                               for r in sorted(_match(rows, m), key=lambda r: r["kind"])],
                } for m in declared],
            }, indent=2))
            return 0
        print(f"models from: {', '.join(str(p) for p in files)}")
        print()
        lines = ["| model | from | cost | kinds | scores | note |",
                 "| --- | --- | --- | --- | --- | --- |"]
        for m in declared:
            matched = sorted(_match(rows, m), key=lambda r: r["kind"])
            scores = ", ".join(f"{r['kind']} {r['score']} ({r['n']})" for r in matched)
            name = f"{m['agent']}:{m['model']}" + (f" ({m['effort']})" if m.get("effort") else "")
            kinds = ", ".join(m["kinds"]) if m.get("kinds") else "all"
            lines.append(f"| {name} | {m['source']} | {m['cost']} | {kinds} "
                         f"| {scores or 'not tried yet'} | {m.get('note') or ''} |")
        print("\n".join(lines))
        return 0

    if args.command == "pick":
        rows = score.table(ledger.read(), args.kind)
        if declared:
            candidates = []
            for m in declared:
                if m.get("kinds") is not None and args.kind not in m["kinds"]:
                    continue
                matched = _match(rows, m)
                candidates += [{**r, "cost": m["cost"]} for r in matched] or [_untried(args.kind, m)]
            rows = sorted(candidates, key=lambda r: (
                score._tier(r), -r["score"] if r["score"] is not None else float("inf"),
                -r["n"], r["executor"]))
        best = score.suggest(rows, args.max_cost)
        if args.json:
            print(json.dumps({"pick": best, "rows": rows[:5]}, indent=2))
        elif best is None and declared:
            within = f" within {args.max_cost}" if args.max_cost else ""
            print(f"no model in your models.toml takes {args.kind} work{within}: "
                  "add one or widen --max-cost")
        elif best is None:
            print(f"no graded {args.kind} jobs yet: use a model you trust and record its score")
        else:
            if best["n"] == 0:
                print(f"pick {args.kind}: {best['executor']} (not tried yet, "
                      f"{best['cost']}) - record its score")
            else:
                sure = best["score"] >= score.GOOD and best["n"] >= score.SURE_N
                print(
                    f"pick {args.kind}: {best['executor']} (score {best['score']}, n {best['n']}, "
                    f"{best['cost'] or 'cost ?'}){'' if sure else ' - not proven yet, try another too'}"
                )
            print()
            print(score.render(rows[:5]))
        return 0

    project = args.project.expanduser().resolve()
    if not (project / "docs" / "crew-chief" / "jobs").is_dir():
        print(f"{project}: no docs/crew-chief/jobs folder")
        return 2
    print(f"imported {ledger.import_v06(project)} grades from {project.name} into {ledger.ledger_path()}")
    return 0
