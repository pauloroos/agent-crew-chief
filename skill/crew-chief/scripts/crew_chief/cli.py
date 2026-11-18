"""``crew-chief.py``: record a graded job, show the scores, pick a model, summarize a run, merge a ledger, sync it,
import 0.6 grades, import an approved spec."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, jobs, launch, ledger, models, score, specs, sync


def _project_name() -> str:
    """The repository's name, the same from any of its worktrees."""
    root = models.repo_root()
    return root.name if root is not None else Path.cwd().name


def _logs_dir() -> Path:
    """The run's logs folder, ``../<repo>-acc-logs`` next to the main checkout."""
    root = models.repo_root() or Path.cwd()
    return root.parent / f"{root.name}-acc-logs"


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
    pick.add_argument("--exclude", action="append", default=[],
                      help="an executor to skip, e.g. codex:m (repeatable)")
    pick.add_argument("--json", action="store_true")

    merged = commands.add_parser("merge", help="copy the grades of another ledger file")
    merged.add_argument("file", type=Path)

    summary = commands.add_parser("summary", help="write this run's job summary before the logs are removed")
    summary.add_argument("--job", required=True, help="the request's slug")
    summary.add_argument("--pr", help="the pull request url")
    summary.add_argument("--rc", type=int, default=0, help="0 when delivered (default), else not")
    summary.add_argument("--logs", type=Path, help="default: ../<repo>-acc-logs")
    summary.add_argument("--project", help="default: this repository's name")
    summary.add_argument("--spec", type=Path, help="the approved spec folder; default: this run's ACC spec-import")

    imported = commands.add_parser("spec-import", help="copy an approved spec byte for byte into the specs folder")
    imported.add_argument("file", type=Path)
    imported.add_argument("--anexo", action="append", default=[], type=Path, help="an attachment (repeatable)")
    imported.add_argument("--slug", required=True)

    synced = commands.add_parser("sync", help="sync the ledger through a private git repo")
    synced.add_argument("--init", default=None, help="the url of a private repo to sync through")

    old = commands.add_parser("import", help="copy the grades of an ACC 0.6 project")
    old.add_argument("project", type=Path)

    started = commands.add_parser("launch", help="start a run on a request: an Orca terminal when Orca runs, "
                                                 "else the background")
    started.add_argument("--brief", required=True, type=Path, help="the request file")
    mode = started.add_mutually_exclusive_group()
    mode.add_argument("--crew", dest="mode", action="store_const", const="crew",
                      help="the agent runs the ACC method (default)")
    mode.add_argument("--direct", dest="mode", action="store_const", const="direct",
                      help="the agent gets the request as is, no ACC")
    mode.add_argument("--engineer", dest="mode", action="store_const", const="engineer",
                      help="start a lane engineer (requires --slug and --lane)")
    started.add_argument("--slug", default=None, help="lane slug (required with --engineer)")
    started.add_argument("--lane", default=None, help="lane name (required with --engineer)")
    started.add_argument("--agent", default="opencode", choices=sorted(launch.AGENTS))
    started.add_argument("--model")
    started.add_argument("--timeout", default="4h", help="kill the run after this (default 4h)")
    started.add_argument("--where", default="auto", choices=launch.WHERE)
    started.add_argument("--title", help="the Orca tab title; default '<acc|direct> <repo> <id>'")
    started.add_argument("--job-dir", type=Path, help="default: ~/.crew-chief/runs")
    started.add_argument("--id", help="the run id; default <UTC time>-<6 hex>")
    started.add_argument("--json", action="store_true")

    listed_runs = commands.add_parser("runs", help="launched runs and their state; one run with its log tail")
    listed_runs.add_argument("id", nargs="?")
    listed_runs.add_argument("--job-dir", type=Path)
    listed_runs.add_argument("--json", action="store_true")

    compared = commands.add_parser("compare", help="crew vs direct runs: finished, ok, with a PR, minutes")
    compared.add_argument("--since", help="YYYY-MM-DD")
    compared.add_argument("--job-dir", type=Path)
    compared.add_argument("--json", action="store_true")
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


def _is_excluded(entry: dict, excludes: list[str]) -> bool:
    """An --exclude value matches score.executor or bare agent:model (every effort)."""
    if score.executor(entry) in excludes:
        return True
    return f"{entry.get('agent')}:{entry.get('model')}" in excludes


def _runs(args) -> int:
    """launch / runs / compare."""
    jobs = args.job_dir or launch.runs_dir()
    try:
        if args.command == "launch":
            if args.mode == "engineer" and (not args.slug or not args.lane):
                print("--engineer requires --slug and --lane", file=sys.stderr)
                return 2
            run = launch.launch(args.brief, mode=args.mode or "crew", agent=args.agent, model=args.model,
                                timeout=args.timeout, job_dir=jobs, job_id=args.id, title=args.title,
                                where=args.where, slug=args.slug, lane=args.lane)
            print(json.dumps(run, indent=2) if args.json else
                  f"run {run['id']} started ({run['mode']}, {run['where']}): {run['title']}\n"
                  f"follow: ACC runs {run['id']}" + (f" --job-dir {args.job_dir}" if args.job_dir else ""))
            return 0
        if args.command == "runs" and args.id:
            got = launch.state(jobs, args.id)
            if args.json:
                print(json.dumps(got, indent=2))
            else:
                print(f"{got['id']}: {got['state']}" + (f" (rc {got['rc']})" if got["rc"] is not None else "")
                      + f", {got['mode']}, {got['where']}, {got['agent']}:{got['model'] or 'default'}")
                print("\n".join(got["tail"]))
            return 0
    except (launch.LaunchError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "runs":
        rows = launch.runs(jobs)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        lines = ["| run | state | mode | where | agent | repo |", "| --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {r['id']} | {r['state']} | {r['mode']} | {r['where']} | {r['agent']}:{r['model'] or 'default'} "
                  f"| {r['repo']} |" for r in rows]
        print("\n".join(lines) if rows else f"no runs in {jobs}")
        return 0
    got = launch.compare(jobs, args.since)
    if args.json:
        print(json.dumps(got, indent=2))
        return 0
    lines = ["| mode | runs | finished | ok | with PR | minutes (median) |", "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {m} | {r['runs']} | {r['finished']} | {r['ok']} | {r['with_pr']} | {r['minutes_median'] or '-'} |"
              for m, r in sorted(got.items())]
    print("\n".join(lines) if got else f"no runs in {jobs}")
    return 0


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
            "machine": ledger.machine(),
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
            print(json.dumps(rows, indent=2) if args.json else score.render(rows, collapse_untried=True))
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
        unlisted: dict[str, list[dict]] = {}
        for row in rows:
            if not any(_match([row], m) for m in declared):
                unlisted.setdefault(row["executor"], []).append(row)
        if unlisted:
            print()
            hints = []
            for name, group in sorted(unlisted.items()):
                kinds = "; ".join(
                    f"{r['kind']} {r['score']}, n {r['n']}"
                    for r in sorted(group, key=lambda r: r["kind"]))
                hints.append(f"{name} ({kinds})")
            print("graded but not listed: " + ", ".join(hints))
        return 0

    if args.command == "pick":
        # the launcher's ceiling reaches every engineer and driver it starts (launch.PASS_ENV)
        env_cost = os.environ.get("CREW_CHIEF_MAX_COST", "").strip()
        if args.max_cost is None and env_cost:
            if env_cost not in ledger.COSTS:
                print(f"CREW_CHIEF_MAX_COST must be one of {', '.join(ledger.COSTS)}: {env_cost!r}", file=sys.stderr)
                return 2
            args.max_cost = env_cost
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
        rows = [r for r in rows if not _is_excluded(r, args.exclude)]
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

    if args.command == "merge":
        try:
            new, already = ledger.merge(args.file)
        except ledger.MergeError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(f"merged {new} new grades from {args.file} ({already} already here)")
        return 0

    if args.command == "summary":
        logs = args.logs if args.logs is not None else _logs_dir()
        line = jobs.summarize(args.project or _project_name(), logs, args.job, args.rc, args.pr, spec=args.spec)
        where = f"jobs/{line['maquina']}/{line['inicio'][:7]}.jsonl"
        print(f"summary {line['id']} {'written to' if jobs.write(line) else 'already in'} {where}")
        if line["spec_divergente"]:
            print("spec_divergente: the committed spec.md, approved.json, the import note and "
                  f"CREW_CHIEF_SPEC_SHA256 do not all match (approved: {line['spec_sha256']})", file=sys.stderr)
            return 1
        return 0

    if args.command == "spec-import":
        try:
            # absolute(), not resolve(): resolve() would follow a symbolic link past the check
            folder = specs.import_spec(args.file.expanduser().absolute(),
                                       [p.expanduser().absolute() for p in args.anexo],
                                       args.slug, specs.checkout())
        except (specs.SpecError, OSError) as exc:
            print(exc, file=sys.stderr)
            return 2
        logs = _logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        # the hash also outside the spec folder: editing spec.md and approved.json together still shows
        note = {"pasta": str(folder), "sha256": specs.sha256(folder / "spec.md"), "fonte": str(args.file)}
        (logs / specs.POINTER).write_text(json.dumps(note) + "\n", encoding="utf-8")
        print(folder)
        return 0

    if args.command == "sync":
        return sync.main(args.init)

    if args.command in ("launch", "runs", "compare"):
        return _runs(args)

    project = args.project.expanduser().resolve()
    if not (project / "docs" / "crew-chief" / "jobs").is_dir():
        print(f"{project}: no docs/crew-chief/jobs folder")
        return 2
    print(f"imported {ledger.import_v06(project)} grades from {project.name} into {ledger.ledger_path()}")
    return 0
