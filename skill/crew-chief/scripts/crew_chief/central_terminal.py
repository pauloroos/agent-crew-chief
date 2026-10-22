"""Pure string rendering for the central terminal view (FR-012, FR-013, FR-024).

This module owns no I/O and no filesystem access: it turns a shared snapshot
dict into a single display string.  ``central_screen`` drives the refreshing
loop; this function only paints.
"""

from __future__ import annotations

from typing import Any

__all__ = ["render"]

_ANSI_SELECTED = "\x1b[1;36m"
_ANSI_RESET = "\x1b[0m"


def _as_text(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if value != value:  # NaN
            return "unknown"
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.3g}"
    return str(value)


def _fmt_number(value: Any, digits: int = 3) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        f = float(value)
        if f != f:
            return "unknown"
        if isinstance(value, int):
            return str(value)
        if f == int(f) and abs(f) < 1e15:
            return str(int(f))
        return f"{f:.{digits}g}"
    return str(value)


def _fmt_ratio(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = float(value)
        if f != f:
            return "unknown"
        if 0 <= f <= 1:
            return f"{f:.0%}"
        return _fmt_number(f)
    return str(value)


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "unknown"
    if isinstance(value, (int, float)):
        f = float(value)
        if f != f:
            return "unknown"
        if f < 0:
            f = 0.0
        if f == int(f) and f < 1e9:
            return f"{int(f)}s"
        return f"{f:.1f}s"
    return str(value)


def _truncate(line: str, width: int) -> str:
    if width is None or width <= 0:
        return line
    if len(line) <= width:
        return line
    if width <= 1:
        return line[:width]
    return line[: width - 1] + "…"


def _metric_value(metrics: Any, name: str) -> Any:
    if not isinstance(metrics, dict):
        return None
    entry = metrics.get(name)
    if isinstance(entry, dict):
        if "value" in entry:
            return entry.get("value")
        return entry
    return entry


def render(snapshot: Any, selected: Any = None, width: int = 80, details: bool = False, plain: bool = False) -> str:
    """Render ``snapshot`` as a single display string.

    ``selected`` highlights one coordinator row when ANSI output is enabled.
    ``plain=True`` disables all ANSI escape sequences.
    """
    if not isinstance(snapshot, dict):
        snapshot = {}

    if isinstance(width, bool):
        width = 80
    if not isinstance(width, int):
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = 80
    if width <= 0:
        width = 80

    selected_id = None
    if selected is not None:
        selected_id = str(selected)

    lines: list[str] = []

    title = "Central terminal"
    lines.append(_truncate(title, width))

    coordinators = snapshot.get("coordinators")
    if isinstance(coordinators, dict):
        coordinators = list(coordinators.values())
    if not isinstance(coordinators, list):
        coordinators = []
    questions = snapshot.get("questions")
    if not isinstance(questions, list):
        questions = []

    # Continuity alerts first (FR-030, US7/AC2).
    alerts = snapshot.get("alerts")
    if isinstance(alerts, list) and alerts:
        lines.append(_truncate("Coordinator alerts:", width))
        for alert in alerts:
            if not isinstance(alert, dict):
                lines.append(_truncate(f"  ! {alert!r}", width))
                continue
            cid = alert.get("coordinator_id", "unknown")
            reason = alert.get("reason", "unknown")
            stopped_at = alert.get("stopped_at") or "unknown"
            quota_reset_at = alert.get("quota_reset_at") or "unknown"
            actions = alert.get("actions") or []
            action_text = "/".join(str(a) for a in actions) if actions else "none"
            lines.append(_truncate(
                f"  ! {cid}: {reason} stopped={stopped_at} reset={quota_reset_at} [{action_text}]",
                width,
            ))
        lines.append("")

    # Pending questions next.
    if questions:
        lines.append(_truncate(f"Pending questions ({len(questions)}):", width))
        for question in questions:
            if not isinstance(question, dict):
                lines.append(_truncate(f"  - {question!r}", width))
                continue
            qid = question.get("id")
            coord = question.get("coordinator_id")
            body = question.get("body")
            refs = question.get("task_refs")
            ref_text = ""
            if isinstance(refs, list) and refs:
                ref_text = " refs=" + ",".join(str(r) for r in refs)
            lines.append(_truncate(f"  ? {qid} (coordinator {coord}){ref_text}", width))
            if body is not None:
                for body_line in str(body).splitlines() or [""]:
                    lines.append(_truncate(f"      {body_line}", width))
    else:
        lines.append(_truncate("Pending questions: none", width))

    if not coordinators:
        lines.append(_truncate("No coordinators.", width))

    for row in coordinators:
        if not isinstance(row, dict):
            lines.append(_truncate(f"- {row!r}", width))
            continue

        row_id = row.get("id")
        is_selected = selected_id is not None and str(row_id) == selected_id

        repo_label = row.get("repo_label")
        if not isinstance(repo_label, str) or not repo_label:
            repo_label = row.get("repo")
        if not isinstance(repo_label, str) or not repo_label:
            repo_label = ""
        else:
            repo_label = str(repo_label)

        cli = row.get("cli")
        state = row.get("state")
        accepted = row.get("accepted")
        total = row.get("total")
        metrics = row.get("metrics")
        stale = bool(row.get("stale"))
        available = row.get("worktree_available")

        mean_score = _metric_value(metrics, "mean_score")
        free_piece = _metric_value(metrics, "free_piece_share")
        free_token = _metric_value(metrics, "free_token_share")

        marker = ">" if is_selected else " "
        head = (
            f"{marker} {repo_label}  cli={_as_text(cli)}  state={_as_text(state)}  "
            f"pieces={_fmt_number(accepted)}/{_fmt_number(total)} (accepted/total)  "
            f"score={_fmt_number(mean_score)}  "
            f"free_share={_fmt_ratio(free_piece)} (pieces), {_fmt_ratio(free_token)} (tokens)  "
            f"free_pieces={_fmt_ratio(free_piece)}  free_tokens={_fmt_ratio(free_token)}"
        )
        if stale:
            head += "  STALE"
        if available is False:
            head += "  (unavailable)"
        last_error = row.get("last_error")
        if last_error is not None:
            head += f"  error={last_error}"

        if is_selected and not plain:
            head = _ANSI_SELECTED + _truncate(head, width) + _ANSI_RESET
        else:
            head = _truncate(head, width)
        lines.append(head)

        active_pieces = row.get("active_pieces")
        if isinstance(active_pieces, list):
            for piece in active_pieces:
                if not isinstance(piece, dict):
                    lines.append(_truncate(f"    - {piece!r}", width))
                    continue
                ref = piece.get("ref")
                title = piece.get("title")
                work_type = piece.get("work_type")
                if work_type is None:
                    work_type = piece.get("type")
                executor = piece.get("executor")
                status = piece.get("status")
                elapsed = piece.get("elapsed_s")
                piece_line = (
                    f"    - {ref} {title}  type={_as_text(work_type)}  "
                    f"exec={_as_text(executor)}  state={_as_text(status)}  "
                    f"time={_fmt_seconds(elapsed)}  elapsed={_fmt_seconds(elapsed)}"
                )
                lines.append(_truncate(piece_line, width))

        if details:
            jobs = row.get("jobs")
            if isinstance(jobs, list) and jobs:
                lines.append(_truncate("    jobs:", width))
                for job in jobs:
                    if not isinstance(job, dict):
                        lines.append(_truncate(f"      - {job!r}", width))
                        continue
                    jid = job.get("id")
                    jtitle = job.get("title")
                    jstate = job.get("state")
                    jstart = job.get("start")
                    jend = job.get("end")
                    lines.append(
                        _truncate(
                            f"      - {jid} {jtitle}  state={_as_text(jstate)}  "
                            f"start={_as_text(jstart)}  end={_as_text(jend)}",
                            width,
                        )
                    )

            models = row.get("models")
            if isinstance(models, list) and models:
                lines.append(_truncate("    models:", width))
                for model in models:
                    if not isinstance(model, dict):
                        lines.append(_truncate(f"      - {model!r}", width))
                        continue
                    role = model.get("role")
                    model_name = model.get("model")
                    tier = model.get("tier")
                    tin = model.get("tokens_in")
                    tout = model.get("tokens_out")
                    ttotal = model.get("tokens_total")
                    coverage = model.get("coverage")
                    estimated = model.get("estimated")
                    lines.append(
                        _truncate(
                            f"      - {role}/{model_name} tier={_as_text(tier)}  "
                            f"in={_fmt_number(tin)} out={_fmt_number(tout)} "
                            f"total={_fmt_number(ttotal)}  coverage={_as_text(coverage)} "
                            f"estimated={_as_text(estimated)}",
                            width,
                        )
                    )

            scoreboard = row.get("scoreboard")
            if isinstance(scoreboard, str) and scoreboard.strip():
                lines.append(_truncate("    scoreboard:", width))
                for sb_line in scoreboard.splitlines():
                    lines.append(_truncate(f"      {sb_line}", width))

            if isinstance(metrics, dict):
                lines.append(_truncate("    metrics:", width))
                for key, value in metrics.items():
                    if isinstance(value, dict) and "numerator" in value and "denominator" in value:
                        lines.append(
                            _truncate(
                                f"      {key}: {_fmt_number(value.get('numerator'))}/"
                                f"{_fmt_number(value.get('denominator'))} = {_fmt_ratio(value.get('value'))}",
                                width,
                            )
                        )

    # Totals
    totals = snapshot.get("totals")
    if isinstance(totals, dict):
        lines.append(_truncate("", width))
        total_line = (
            f"Totals: coordinators={_fmt_number(totals.get('coordinators'))}  "
            f"jobs={_fmt_number(totals.get('jobs'))}  "
            f"questions={_fmt_number(totals.get('questions'))}  "
            f"pieces={_fmt_number(totals.get('pieces'))}/{_fmt_number(totals.get('accepted_pieces'))} accepted  "
            f"score={_fmt_number(totals.get('mean_score'))}"
        )
        lines.append(_truncate(total_line, width))

    diagnostics = snapshot.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        lines.append(_truncate(f"Diagnostics ({len(diagnostics)}):", width))
        for diag in diagnostics:
            if isinstance(diag, dict):
                lines.append(
                    _truncate(
                        f"  - {diag.get('record')}: {diag.get('message')}",
                        width,
                    )
                )
            else:
                lines.append(_truncate(f"  - {diag!r}", width))

    return "\n".join(lines)
