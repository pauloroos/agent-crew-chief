"""``crew-chief.py``: record a graded job, show the scores, pick a model, import 0.6 grades."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from . import __version__, ledger, score


def _project_name() -> str:
    """The repository's name, the same from any of its worktrees."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd().name
    return Path(common).parent.name


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

    pick = commands.add_parser("pick", help="the model to use for a kind of work")
    pick.add_argument("--kind", required=True, choices=ledger.KINDS)
    pick.add_argument("--max-cost", choices=ledger.COSTS)
    pick.add_argument("--json", action="store_true")

    old = commands.add_parser("import", help="copy the grades of an ACC 0.6 project")
    old.add_argument("project", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

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
        print(json.dumps(rows, indent=2) if args.json else score.render(rows))
        return 0

    if args.command == "pick":
        rows = score.table(ledger.read(), args.kind)
        best = score.suggest(rows, args.max_cost)
        if args.json:
            print(json.dumps({"pick": best, "rows": rows[:5]}, indent=2))
        elif best is None:
            print(f"no graded {args.kind} jobs yet: use a model you trust and record its score")
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
