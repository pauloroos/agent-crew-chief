"""Interview engine for sequential question ordering (T006, US2, FR-011).

Yields interview questions in the exact order:
project, request, coordinator, executors, end_mode.

Preference resolution (T007, US2, FR-012, FR-010):
Questions are skipped when a matching key exists in project_prefs or
global_prefs. Built-in defaults do NOT cause a skip. Source attribution
(project, global, or user) is recorded on each Question.

Zero-spend gating (T008, US2, FR-013):
Dispatch refuses to create a worktree, spawn a process, or consume tokens
while any interview question remains unanswered.

Interview expiration (T009, US2, FR-014):
Jobs whose interview started more than timeout_s ago (default 24h) transition
to state "expired" and emit an interview_expired lifecycle event. Dispatch
is refused on expired interviews just like unanswered ones.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DEFAULT_INTERVIEW_TIMEOUT_S = 86400  # 24 hours


class InterviewPendingError(ValueError):
    """Raised when dispatch or execution is attempted while interview questions remain unanswered."""


class InterviewExpiredError(InterviewPendingError):
    """Raised when dispatch is attempted on an interview that has exceeded its timeout."""


class Question:
    """A single interview question with key, prompt, validation and defaults."""

    __slots__ = (
        "key",
        "prompt",
        "validators",
        "default",
        "skip_when",
        "resolved_value",
        "resolved_source",
    )

    def __init__(
        self,
        key,
        prompt,
        validators=None,
        default=None,
        skip_when=None,
        resolved_value=None,
        resolved_source="user",
    ):
        self.key = key
        self.prompt = prompt
        self.validators = list(validators or [])
        self.default = default
        self.skip_when = skip_when
        self.resolved_value = resolved_value
        self.resolved_source = resolved_source


_QUESTION_DEFS = [
    {
        "key": "project",
        "prompt": "Project name or identifier",
        "validators": ["required"],
    },
    {
        "key": "request",
        "prompt": "Request description or brief reference",
        "validators": ["required"],
    },
    {
        "key": "coordinator",
        "prompt": "Coordinator agent or role",
        "validators": [],
        "default": None,
    },
    {
        "key": "executors",
        "prompt": "Executor agents or pool",
        "validators": [],
        "default": None,
    },
    {
        "key": "end_mode",
        "prompt": "End mode (pr, pr_review)",
        "validators": ["required"],
        "default": None,
    },
]


class InterviewEngine:
    """Produces interview questions in a fixed canonical order.

    Accepts optional project_prefs and global_prefs dicts. When a question's
    key is found in either dict, the question is marked for skipping with
    the resolved value and source attribution. Project prefs take precedence
    over global prefs. Built-in defaults alone do not trigger a skip.
    """

    def __init__(self, project_prefs=None, global_prefs=None):
        self._project_prefs = dict(project_prefs or {})
        self._global_prefs = dict(global_prefs or {})

    def questions(self):
        """Yield Question objects in project/request/coordinator/executors/end_mode order."""
        for defn in _QUESTION_DEFS:
            key = defn["key"]
            resolved_value = None
            resolved_source = "user"
            skip_when = None

            if key in self._project_prefs:
                resolved_value = self._project_prefs[key]
                resolved_source = "project"
                skip_when = lambda: True
            elif key in self._global_prefs:
                resolved_value = self._global_prefs[key]
                resolved_source = "global"
                skip_when = lambda: True

            yield Question(
                key=key,
                prompt=defn["prompt"],
                validators=defn.get("validators"),
                default=defn.get("default"),
                skip_when=skip_when,
                resolved_value=resolved_value,
                resolved_source=resolved_source,
            )

    def is_question_answered(self, question: Question, answers: dict[str, Any] | None = None) -> bool:
        """Return True if question is skipped by prefs or explicitly answered."""
        if question.skip_when and question.skip_when():
            return True
        if not answers or not isinstance(answers, dict):
            return False
        val = answers.get(question.key)
        if isinstance(val, dict):
            val = val.get("value")
        return val is not None and val != ""

    def pending_questions(self, answers: dict[str, Any] | None = None) -> list[Question]:
        """Return questions not skipped by prefs and without an answer."""
        return [q for q in self.questions() if not self.is_question_answered(q, answers)]

    def is_complete(self, answers: dict[str, Any] | None = None) -> bool:
        """Return True if all questions are answered or skipped by prefs."""
        return len(self.pending_questions(answers)) == 0

    def check_gate(self, answers: dict[str, Any] | None = None) -> None:
        """Raise InterviewPendingError if any interview question remains unanswered."""
        pending = self.pending_questions(answers)
        if pending:
            keys = ", ".join(q.key for q in pending)
            raise InterviewPendingError(
                f"cannot dispatch: interview has unanswered questions ({keys})"
            )


def _parse_started_at(value: Any) -> datetime | None:
    """Parse started_at as ISO-8601 UTC string or float epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def check_interview_expiration(
    job: dict[str, Any],
    timeout_s: int = DEFAULT_INTERVIEW_TIMEOUT_S,
    now: datetime | None = None,
    project: Any = None,
    job_id: str | None = None,
) -> bool:
    """Return True and transition job to expired when interview exceeds timeout.

    Reads job["interview"]["started_at"]; missing values are treated as
    not-expired for backward compatibility. Expiration only applies while the
    interview is still open. On expiration, sets job["state"] = "expired" and
    appends an interview_expired lifecycle event.
    """
    # An expired job stays expired: reject further answers and never launch.
    # This check must come first; the guards below return False for any
    # non-interview state, which would otherwise make this unreachable.
    if job.get("state") == "expired" or job.get("status") == "expired":
        return True

    interview = job.get("interview")
    if not isinstance(interview, dict):
        return False

    # Expiry only applies while the interview is still open.
    # If the interview has finished (e.g. pending list is empty or state is no
    # longer interview), do not expire.
    pending = interview.get("pending")
    if pending is not None and len(pending) == 0:
        return False
    state = job.get("state")
    if state is not None and state != "interview":
        return False

    started_at = _parse_started_at(interview.get("started_at"))
    if started_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    elapsed = (current - started_at).total_seconds()
    if elapsed <= timeout_s:
        return False

    events = job.get("events")
    if not isinstance(events, list):
        events = []
        job["events"] = events
    events.append({"type": "interview_expired", "at": current.isoformat()})

    from . import records
    records.transition_terminal_state(
        job=job,
        project=project,
        job_id=job_id or job.get("id"),
        new_state="expired",
        extra_fields={"at": current.isoformat()},
    )
    return True


def check_interview_gate(
    job: dict[str, Any],
    prefs: Any = None,
    project_prefs: dict[str, Any] | None = None,
    global_prefs: dict[str, Any] | None = None,
    timeout_s: int = DEFAULT_INTERVIEW_TIMEOUT_S,
) -> None:
    """Refuse dispatch if the job has an active interview with unanswered questions.

    Raises InterviewExpiredError when the interview has exceeded its timeout,
    transitioning the job to state 'expired' and emitting a lifecycle event.
    Raises InterviewPendingError if questions are pending.
    """
    is_interview_job = (
        job.get("state") == "interview"
        or job.get("interview_complete") is False
        or "interview" in job
    )
    if not is_interview_job:
        return

    if check_interview_expiration(job, timeout_s=timeout_s):
        raise InterviewExpiredError(
            "cannot dispatch: interview has expired after configured timeout"
        )

    resolved_proj: dict[str, Any] = dict(project_prefs or {})
    if not resolved_proj and prefs is not None:
        if hasattr(prefs, "project_prefs") and prefs.project_prefs:
            resolved_proj.update(prefs.project_prefs)
        elif isinstance(prefs, dict) and prefs.get("project_prefs"):
            resolved_proj.update(prefs["project_prefs"])
    if not resolved_proj and isinstance(job.get("project_prefs"), dict):
        resolved_proj.update(job["project_prefs"])

    resolved_glob: dict[str, Any] = dict(global_prefs or {})
    if not resolved_glob and prefs is not None:
        if hasattr(prefs, "global_prefs") and prefs.global_prefs:
            resolved_glob.update(prefs.global_prefs)
        elif isinstance(prefs, dict) and prefs.get("global_prefs"):
            resolved_glob.update(prefs["global_prefs"])
    if not resolved_glob and isinstance(job.get("global_prefs"), dict):
        resolved_glob.update(job["global_prefs"])

    answers: dict[str, Any] = {}
    if isinstance(job.get("interview"), dict):
        if "answers" in job["interview"] and isinstance(job["interview"]["answers"], dict):
            answers.update(job["interview"]["answers"])
        else:
            answers.update(job["interview"])
    if isinstance(job.get("answers"), dict):
        answers.update(job["answers"])

    engine = InterviewEngine(project_prefs=resolved_proj, global_prefs=resolved_glob)
    engine.check_gate(answers)
