"""Argument parser construction for the crew-chief CLI."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import cli

__all__ = ["build_parser"]


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every subcommand."""
    from . import cli

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--project",
        metavar="PATH",
        default=None,
        help="project root (default: the current directory)",
    )
    common.add_argument(
        "--json", action="store_true", help="print the result as JSON"
    )

    parser = argparse.ArgumentParser(
        prog="crew-chief", description="Coordinate several executor CLIs."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {cli.__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    discover_parser = subcommands.add_parser(
        "discover", parents=[common], help="list the executors available now"
    )
    discover_parser.set_defaults(handler=cli._cmd_discover)

    interview_parser = subcommands.add_parser(
        "interview", parents=[common], help="start or inspect an interview-mode job (T036, FR-036)"
    )
    interview_parser.add_argument(
        "--projects-dir",
        metavar="PATH",
        default=None,
        help="trusted allowed directory for projects",
    )
    interview_parser.add_argument("--job", default=None, help="job ID to query or inspect")
    interview_parser.add_argument(
        "--cursor", type=int, default=None, help="event replay cursor"
    )
    interview_parser.add_argument(
        "--request-file", metavar="F", default=None, help="UTF-8 request file"
    )
    interview_parser.add_argument("--request", default=None, help="request description")
    interview_parser.add_argument("--title", default=None, help="job title")
    interview_parser.add_argument("--coordinator", default=None, help="coordinator")
    interview_parser.add_argument("--executors", default=None, help="executor mode")
    interview_parser.add_argument("--end-mode", default=None, help="end mode")
    interview_parser.add_argument(
        "--answer", metavar="KEY=VALUE", default=None, help="answer one pending question of --job"
    )
    interview_parser.set_defaults(handler=cli._cmd_interview)

    job_parser = subcommands.add_parser("job", help="job records")
    job_commands = job_parser.add_subparsers(dest="job_command", required=True)
    job_new = job_commands.add_parser(
        "new",
        aliases=["create"],
        parents=[common],
        help="create a job from a request file",
    )
    job_new.add_argument("--title", required=True, help="job title")
    job_new.add_argument(
        "--request-file", metavar="F", required=True, help="UTF-8 request file"
    )
    job_new.add_argument(
        "--not-distributed",
        action="store_true",
        help="the job stays with the coordinator",
    )
    job_new.add_argument("--reason", default="", help="why the job was not distributed")
    job_new.add_argument("--issue", default="", help="issue tracker reference")
    job_new.add_argument(
        "--executors",
        choices=cli.records.EXECUTOR_MODES,
        default=None,
        help="executor mode: free_first, own or subagents",
    )
    job_new.add_argument(
        "--superpowers",
        choices=cli.records.SUPERPOWERS_VALUES,
        default=None,
        help="superpowers mode or skill: off, auto, or a specific skill",
    )
    job_new.set_defaults(handler=cli._cmd_job_new)

    job_set = job_commands.add_parser(
        "set", parents=[common], help="update mutable job fields"
    )
    job_set.add_argument("--job", required=True, help="job id")
    job_set.add_argument(
        "--executors",
        choices=cli.records.EXECUTOR_MODES,
        default=None,
        help="executor mode: free_first, own or subagents",
    )
    job_set.set_defaults(handler=cli._cmd_job_set)

    job_show = job_commands.add_parser(
        "show", parents=[common], help="display job details"
    )
    job_show.add_argument("--job", required=True, help="job id")
    job_show.add_argument(
        "--coordinator",
        default=None,
        help="coordinator CLI name",
    )
    job_show.set_defaults(handler=cli._cmd_job_show)

    piece_parser = subcommands.add_parser("piece", help="job pieces")
    piece_commands = piece_parser.add_subparsers(dest="piece_command", required=True)
    piece_add = piece_commands.add_parser(
        "add", parents=[common], help="append a piece to a job"
    )
    piece_add.add_argument("--job", required=True, help="job id")
    piece_add.add_argument("--title", required=True, help="piece title")
    piece_add.add_argument("--type", required=True, help="work type")
    piece_add.add_argument("--risk", required=True, help="risk level")
    piece_add.add_argument(
        "--superpowers",
        choices=cli.records.SUPERPOWERS_VALUES,
        default=None,
        help="superpowers mode or skill: off, auto, or a specific skill",
    )
    piece_add.set_defaults(handler=cli._cmd_piece_add)

    piece_start = piece_commands.add_parser(
        "start", parents=[common], help="create an isolated worktree for a native piece"
    )
    piece_start.add_argument("--job", required=True, help="job id")
    piece_start.add_argument(
        "--piece", type=int, required=True, help="piece number"
    )
    piece_start.add_argument(
        "--native", action="store_true", help="native subagent isolation (required)"
    )
    piece_start.add_argument(
        "--coordinator",
        default=None,
        metavar="TOOL",
        help=f"coordinator CLI name (default: ${cli._COORDINATOR_ENV})",
    )
    piece_start.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="model name for the native executor id",
    )
    piece_start.add_argument(
        "--base",
        default=None,
        metavar="N|BRANCH",
        help="start the worktree from this attempt number of the piece or "
        "this branch (default: HEAD, or the last ok attempt once a correction "
        "was recorded after it)",
    )
    piece_start.add_argument(
        "--fresh",
        action="store_true",
        help="start the worktree from HEAD even after a correction",
    )
    piece_start.add_argument(
        "--ignore-deps",
        action="store_true",
        help="start a spec piece although a dependency or an earlier phase "
        "is not accepted yet",
    )
    piece_start.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="seconds to wait for an executor slot before failing (default: 5.0)",
    )
    piece_start.set_defaults(handler=cli._cmd_piece_start)

    piece_finish = piece_commands.add_parser(
        "finish", parents=[common], help="finish a native piece: diff, proof, isolation check"
    )
    piece_finish.add_argument("--job", required=True, help="job id")
    piece_finish.add_argument(
        "--piece", type=int, required=True, help="piece number"
    )
    piece_finish.add_argument(
        "--ignore-deps",
        action="store_true",
        help="finish a spec piece although a dependency or an earlier phase "
        "is not accepted yet",
    )
    piece_finish.add_argument(
        "--no-proof",
        action="store_true",
        help="skip proof verification",
    )
    piece_finish.add_argument(
        "--proof",
        default=None,
        metavar="CMD",
        help="override proof command",
    )
    piece_finish.add_argument(
        "--proof-expect",
        default=None,
        metavar="TEXT",
        help="override expected proof output",
    )
    piece_finish.set_defaults(handler=cli._cmd_piece_finish)

    pick_parser = subcommands.add_parser(
        "pick", parents=[common], help="choose the next executor for a piece"
    )
    pick_parser.add_argument("--job", required=True, help="job id")
    pick_parser.add_argument("--piece", type=int, required=True, help="piece number")
    pick_parser.add_argument(
        "--exclude",
        nargs="+",
        action="extend",
        default=[],
        metavar="EXECUTOR",
        help="executor ids to leave out (repeat the flag to add more)",
    )
    pick_parser.add_argument(
        "--strong",
        action="store_true",
        help="the piece is complex and cannot be split (the only way execution "
        "work reaches a paid executor)",
    )
    pick_parser.add_argument(
        "--review",
        action="store_true",
        help="pick a reviewer for the piece: thinking work, outside the quota "
        "family of the executor that did it",
    )
    pick_parser.add_argument(
        "--coordinator",
        default=None,
        metavar="TOOL",
        help=f"the CLI coordinating this job (default: ${cli._COORDINATOR_ENV}): "
        "its included executors are used last",
    )
    pick_parser.add_argument(
        "--allow-own-cli",
        action="store_true",
        help="do not put the coordinator's own CLI last among the included "
        "executors",
    )
    pick_parser.set_defaults(handler=cli._cmd_pick)

    dispatch_parser = subcommands.add_parser(
        "dispatch", parents=[common], help="run one attempt"
    )
    dispatch_parser.add_argument("--job", required=True, help="job id")
    dispatch_parser.add_argument(
        "--piece", type=int, required=True, help="piece number"
    )
    dispatch_parser.add_argument("--executor", required=True, help="executor id")
    dispatch_parser.add_argument(
        "--brief-file", metavar="F", required=True, help="UTF-8 brief file"
    )
    dispatch_parser.add_argument(
        "--review", action="store_true", help="run as a review, not as exec"
    )
    dispatch_parser.add_argument(
        "--target", default=None, help="worktree a review must run in"
    )
    start = dispatch_parser.add_mutually_exclusive_group()
    start.add_argument(
        "--base",
        default=None,
        metavar="N|BRANCH",
        help="start the worktree from this attempt number of the piece or "
        "this branch (default: HEAD, or the last ok attempt once a correction "
        "was recorded after it)",
    )
    start.add_argument(
        "--fresh",
        action="store_true",
        help="start the worktree from HEAD even after a correction",
    )
    dispatch_parser.add_argument(
        "--no-proof",
        action="store_true",
        help="dispatch a spec piece without running its proof (recorded as skipped)",
    )
    dispatch_parser.add_argument(
        "--ignore-deps",
        action="store_true",
        help="dispatch a spec piece although a dependency or an earlier phase "
        "is not accepted yet (recorded as ignored_deps)",
    )
    dispatch_parser.add_argument(
        "--effort",
        default=None,
        help="effort/reasoning (default: the effort pick prints)",
    )
    dispatch_parser.add_argument(
        "--timeout", type=int, default=None, metavar="S", help="timeout in seconds"
    )
    dispatch_parser.add_argument(
        "--superpowers",
        choices=cli.records.SUPERPOWERS_VALUES,
        default=None,
        help="superpowers mode or skill for this attempt",
    )
    dispatch_parser.set_defaults(handler=cli._cmd_dispatch)

    correction_parser = subcommands.add_parser(
        "correction", parents=[common], help="record a correction on a piece"
    )
    correction_parser.add_argument("--job", required=True, help="job id")
    correction_parser.add_argument(
        "--piece", type=int, required=True, help="piece number"
    )
    correction_parser.add_argument("--reason", required=True, help="what went wrong")
    correction_parser.add_argument("--executor", default=None, help="executor id")
    correction_parser.set_defaults(handler=cli._cmd_correction)

    grade_parser = subcommands.add_parser(
        "grade", parents=[common], help="grade a piece and refresh the scoreboard"
    )
    grade_parser.add_argument("--job", required=True, help="job id")
    grade_parser.add_argument("--piece", type=int, required=True, help="piece number")
    grade_parser.add_argument("--executor", required=True, help="executor id")
    grade_parser.add_argument(
        "--corrections", type=int, required=True, help="number of corrections"
    )
    grade_parser.add_argument("--done", type=int, required=True, help="items done")
    grade_parser.add_argument("--missing", type=int, required=True, help="items missing")
    grade_parser.add_argument("--extra", type=int, required=True, help="items added")
    grade_parser.add_argument(
        "--penalty",
        action="append",
        default=[],
        metavar="P",
        help="named penalty (repeatable)",
    )
    grade_parser.set_defaults(handler=cli._cmd_grade)

    accept_parser = subcommands.add_parser(
        "accept", parents=[common], help="merge the piece's last ok attempt"
    )
    accept_parser.add_argument("--job", required=True, help="job id")
    accept_parser.add_argument("--piece", type=int, required=True, help="piece number")
    accept_parser.add_argument(
        "--ignore-deps",
        action="store_true",
        help="accept a spec piece although a dependency or an earlier phase "
        "is not accepted yet",
    )
    accept_parser.set_defaults(handler=cli._cmd_accept)

    discard_parser = subcommands.add_parser(
        "discard", parents=[common], help="drop the piece's last ok attempt"
    )
    discard_parser.add_argument("--job", required=True, help="job id")
    discard_parser.add_argument(
        "--piece", type=int, required=True, help="piece number"
    )
    discard_parser.set_defaults(handler=cli._cmd_discard)

    close_parser = subcommands.add_parser(
        "close", parents=[common], help="print the job's commit message and comment"
    )
    close_parser.add_argument("--job", required=True, help="job id")
    close_parser.add_argument(
        "--no-changelog",
        action="store_true",
        help="do not add the job to the project CHANGELOG",
    )
    close_parser.add_argument(
        "--type",
        choices=cli.records.CLOSE_TYPES,
        default=None,
        help="commit type and CHANGELOG category (default: a 'fix: ' style "
        "prefix of the job title, else the first piece's work type)",
    )
    close_parser.add_argument(
        "--end-mode",
        choices=("pr", "pr_review"),
        default=None,
        help="how to deliver the job: pr (open a pull request) or pr_review (post a review comment without approving)",
    )
    close_parser.set_defaults(handler=cli._cmd_close)

    spec_parser = subcommands.add_parser(
        "spec", help="spec-kit specs: new folder, import tasks as pieces, status"
    )
    spec_commands = spec_parser.add_subparsers(dest="spec_command", required=True)
    spec_new = spec_commands.add_parser(
        "new", parents=[common], help="create specs/NNN-<slug>/ from the templates"
    )
    spec_new.add_argument("--title", required=True, help="feature title")
    spec_new.set_defaults(handler=cli._cmd_spec_new)
    spec_import = spec_commands.add_parser(
        "import", parents=[common], help="turn open tasks.md tasks into pieces"
    )
    spec_import.add_argument("--job", required=True, help="job id")
    spec_import.add_argument(
        "--spec-dir",
        default=None,
        metavar="PATH",
        help="spec folder (default: the job's, else .specify/feature.json)",
    )
    spec_import.add_argument(
        "--superpowers",
        choices=cli.records.SUPERPOWERS_VALUES,
        default=None,
        help="superpowers mode or skill for imported pieces",
    )
    spec_import.set_defaults(handler=cli._cmd_spec_import)
    spec_status = spec_commands.add_parser(
        "status", parents=[common], help="progress per phase, ready and blocked tasks"
    )
    spec_status.add_argument("--job", required=True, help="job id")
    spec_status.add_argument(
        "--spec-dir", default=None, metavar="PATH", help="spec folder"
    )
    spec_status.set_defaults(handler=cli._cmd_spec_status)

    brief_parser = subcommands.add_parser(
        "brief", help="briefs for spec tasks: exec, review, converge"
    )
    brief_commands = brief_parser.add_subparsers(dest="brief_command", required=True)
    brief_render = brief_commands.add_parser(
        "render", parents=[common], help="render the brief of a spec piece"
    )
    brief_render.add_argument("--job", required=True, help="job id")
    brief_render.add_argument(
        "--piece", type=int, default=None, help="piece number (not needed with --converge)"
    )
    brief_render.add_argument(
        "--review", action="store_true", help="the reviewer brief (two verdicts)"
    )
    brief_render.add_argument(
        "--attempt",
        type=int,
        default=None,
        metavar="N",
        help="with --review: the exec attempt to review (default: the last ok one)",
    )
    brief_render.add_argument(
        "--converge", action="store_true", help="the final spec-vs-code comparison brief"
    )
    brief_render.add_argument(
        "--out", default=None, metavar="FILE", help="write the brief here, not to stdout"
    )
    brief_render.set_defaults(handler=cli._cmd_brief_render)

    ruling_parser = subcommands.add_parser("ruling", help="rulings recorded in the job")
    ruling_commands = ruling_parser.add_subparsers(dest="ruling_command", required=True)
    ruling_add = ruling_commands.add_parser(
        "add", parents=[common], help="record a ruling in the job"
    )
    ruling_add.add_argument("--job", required=True, help="job id")
    ruling_add.add_argument(
        "--text", required=True, help='"<decision> — <why> — <cost if wrong>"'
    )
    ruling_add.set_defaults(handler=cli._cmd_ruling_add)

    decision_parser = subcommands.add_parser("decision", help="ADRs in docs/decisions")
    decision_commands = decision_parser.add_subparsers(
        dest="decision_command", required=True
    )
    decision_add = decision_commands.add_parser(
        "add", parents=[common], help="write an ADR (from a ruling or a title)"
    )
    decision_add.add_argument("--job", required=True, help="job id")
    decision_add.add_argument(
        "--from-ruling", type=int, default=None, metavar="N", help="promote ruling N"
    )
    decision_add.add_argument("--title", default=None, help="ADR title (no ruling)")
    decision_add.add_argument("--context", default="", help="why this came up")
    decision_add.add_argument("--alternatives", default="", help="what was weighed")
    decision_add.add_argument("--consequences", default="", help="what follows")
    decision_add.set_defaults(handler=cli._cmd_decision_add)

    usage_parser = subcommands.add_parser(
        "usage", help="the coordinator's own token spend"
    )
    usage_commands = usage_parser.add_subparsers(dest="usage_command", required=True)
    usage_add = usage_commands.add_parser(
        "add", parents=[common], help="record the coordinator's tokens"
    )
    usage_add.add_argument("--job", required=True, help="job id")
    usage_add.add_argument(
        "--role", required=True, choices=cli.records.USAGE_ROLES, help="who spent them"
    )
    usage_add.add_argument("--tokens-in", type=int, required=True, help="input tokens")
    usage_add.add_argument(
        "--tokens-out", type=int, required=True, help="output tokens"
    )
    usage_add.add_argument(
        "--estimated", action="store_true", help="the counts are an estimate"
    )
    usage_add.add_argument("--note", default="", help="what the tokens were for")
    usage_add.set_defaults(handler=cli._cmd_usage_add)

    economy_parser = subcommands.add_parser(
        "economy", parents=[common], help="free share, coordinator share, rework, time"
    )
    economy_parser.add_argument("--job", required=True, help="job id")
    economy_parser.set_defaults(handler=cli._cmd_economy)

    scoreboard_parser = subcommands.add_parser(
        "scoreboard", parents=[common], help="regenerate and print the scoreboard"
    )
    scoreboard_parser.set_defaults(handler=cli._cmd_scoreboard)

    setup_parser = subcommands.add_parser(
        "setup", parents=[common], help="detect the CLIs and the subscriptions, "
        "ask the rest and write the global cost tiers"
    )
    setup_parser.add_argument(
        "--yes",
        action="store_true",
        help="ask nothing: accept every default (subscriptions no, budget 0, "
        "no training)",
    )
    setup_parser.add_argument(
        "--lang",
        choices=("pt", "en"),
        default=None,
        help="language of the questions (default: the system language)",
    )
    setup_parser.set_defaults(handler=cli._cmd_setup)

    # --- maestro: one coordinator per front -----------------------------------
    launch_parser = subcommands.add_parser(
        "launch", parents=[common], help="start a coordinator for one front in its own worktree"
    )
    launch_parser.add_argument("--request-file", metavar="F", required=True, help="the front's request")
    launch_parser.add_argument("--spec-dir", default=None, help="approved spec directory to import")
    launch_parser.add_argument(
        "--coordinator", metavar="TOOL:MODEL", action="append", default=None,
        help="coordinator to use, repeat for the fallback order (default: [team].coordinators)",
    )
    launch_parser.add_argument("--executors", default=None, help="executor mode of the job")
    launch_parser.add_argument("--base", default="HEAD", help="start the branch from this ref")
    launch_parser.set_defaults(handler=cli._cmd_launch)

    status_parser = subcommands.add_parser(
        "status", parents=[common], help="coordinators of this project: state, rounds, time, tokens, score"
    )
    status_parser.set_defaults(handler=cli._cmd_status)

    resume_parser = subcommands.add_parser(
        "resume", parents=[common], help="run a held, asking or failed coordinator again"
    )
    resume_parser.add_argument("coordinator_id")
    resume_parser.add_argument("--answer", default=None, help="answer to its question.md")
    resume_parser.set_defaults(handler=cli._cmd_resume)

    stop_parser = subcommands.add_parser(
        "stop", parents=[common], help="stop a coordinator; its worktree and branch stay"
    )
    stop_parser.add_argument("coordinator_id")
    stop_parser.set_defaults(handler=cli._cmd_stop)

    clean_parser = subcommands.add_parser(
        "clean", parents=[common],
        help="remove the worktrees and attempt branches of finished fronts; their numbers stay",
    )
    clean_parser.add_argument(
        "coordinator_ids", nargs="*", help="fronts to clean (default: every completed front not cleaned yet)"
    )
    clean_parser.set_defaults(handler=cli._cmd_clean)

    return parser
