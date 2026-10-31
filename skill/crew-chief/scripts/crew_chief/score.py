"""Scores per model and kind of work, read from the ledger, and the pick they suggest."""

from __future__ import annotations

from datetime import datetime, timezone

from .ledger import COSTS

#: A grade counts half after this many days, so a model that changed upstream recovers.
HALF_LIFE_DAYS = 30
#: One imaginary grade of 50 in every score: a single 100 is not a 100.
PRIOR_SCORE, PRIOR_WEIGHT = 50.0, 1.0
#: The cheapest model with at least this score over SURE_N jobs is the suggestion.
GOOD, SURE_N = 70, 2


def executor(entry: dict) -> str:
    name = f"{entry.get('agent')}:{entry.get('model')}"
    return f"{name} ({entry['effort']})" if entry.get("effort") else name


def _age_days(at: object, now: datetime) -> float:
    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((now - when).total_seconds() / 86400, 0.0)


def table(
    entries: list[dict],
    kind: str | None = None,
    project: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """One row per kind and model, best score first within each kind."""
    now = now or datetime.now(timezone.utc)
    groups: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        if kind and entry.get("kind") != kind or project and entry.get("project") != project:
            continue
        if not isinstance(entry.get("score"), (int, float)):
            continue
        groups.setdefault((str(entry.get("kind")), executor(entry)), []).append(entry)

    rows = []
    for (group_kind, name), group in groups.items():
        weights = [0.5 ** (_age_days(e.get("at"), now) / HALF_LIFE_DAYS) for e in group]
        weighted = sum(w * e["score"] for w, e in zip(weights, group))
        score = (weighted + PRIOR_SCORE * PRIOR_WEIGHT) / (sum(weights) + PRIOR_WEIGHT)
        minutes = [e["minutes"] for e in group if isinstance(e.get("minutes"), (int, float))]
        last = max(group, key=lambda e: str(e.get("at", "")))
        rows.append({
            "kind": group_kind,
            "executor": name,
            "agent": last.get("agent"),
            "model": last.get("model"),
            "effort": last.get("effort"),
            "cost": last.get("cost"),
            "score": round(score, 1),
            "n": len(group),
            "pass_rate": round(sum(e.get("outcome") == "pass" for e in group) / len(group), 2),
            "minutes": round(sum(minutes) / len(minutes), 1) if minutes else None,
            "last": str(last.get("at", ""))[:10],
        })
    rows.sort(key=lambda r: (r["kind"], -r["score"], -r["n"]))
    return rows


def _tier(row: dict) -> int:
    # a cost nobody recorded sorts as the dearest
    return COSTS.index(row["cost"]) if row.get("cost") in COSTS else len(COSTS)


def suggest(rows: list[dict], max_cost: str | None = None) -> dict | None:
    """The cheapest model that is surely good, else the best score so far, else None."""
    if max_cost:
        rows = [r for r in rows if _tier(r) <= COSTS.index(max_cost)]
    sure = [r for r in rows if r["score"] >= GOOD and r["n"] >= SURE_N]
    if sure:
        return min(sure, key=lambda r: (_tier(r), -r["score"]))
    return max(rows, key=lambda r: (r["score"], -_tier(r)), default=None)


def render(rows: list[dict]) -> str:
    """The rows as one markdown table per kind."""
    lines: list[str] = []
    kind = None
    for row in rows:
        if row["kind"] != kind:
            kind = row["kind"]
            if lines:
                lines.append("")
            lines += [
                f"## {kind}",
                "",
                "| model | score | n | pass | minutes | cost | last |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        minutes = "-" if row["minutes"] is None else row["minutes"]
        lines.append(
            f"| {row['executor']} | {row['score']} | {row['n']} | {round(row['pass_rate'] * 100)}% "
            f"| {minutes} | {row['cost'] or '?'} | {row['last']} |"
        )
    return "\n".join(lines) if lines else "No graded jobs yet."
