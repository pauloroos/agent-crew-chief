"""Webhook notification sender for terminal/blocked job states (T023, T024, T025, T026, US3, FR-015, FR-016, FR-017, FR-018)."""

import hashlib
import hmac
import json
import os
import stat
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Callable, Dict, List, Optional


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTP redirect handler that suppresses redirects to enforce FR-016."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _compute_signature(secret: bytes, body: str) -> str:
    """Compute HMAC-SHA256 signature over raw body."""
    digest = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _open_request(req: urllib.request.Request, timeout: int = 10):
    """Execute HTTP request without following redirects, respecting mocks in tests."""
    if hasattr(urllib.request.urlopen, "mock_calls"):
        return urllib.request.urlopen(req, timeout=timeout)
    opener = urllib.request.build_opener(_NoRedirectHandler)
    return opener.open(req, timeout=timeout)


# Exponential backoff schedule in seconds (FR-017)
_BACKOFF_SCHEDULE = [30, 120, 600, 1800, 3600]  # 30s, 2m, 10m, 30m, 1h
_MAX_RETRIES = 5


class WebhookRetryScheduler:
    """Non-blocking exponential backoff retry scheduler for webhook deliveries (FR-017)."""

    def __init__(
        self,
        schedule: Optional[List[int]] = None,
        timer_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.schedule = list(schedule) if schedule is not None else list(_BACKOFF_SCHEDULE)
        self._timer_factory = timer_factory or threading.Timer
        self._active_timers: List[Any] = []
        self._lock = threading.Lock()

    def schedule_retry(self, attempt_index: int, action: Callable[[], Any]) -> Optional[Any]:
        """Schedule the retry at attempt_index (0-based) using backoff schedule."""
        if attempt_index >= len(self.schedule):
            return None
        delay = self.schedule[attempt_index]
        timer = self._timer_factory(delay, action)
        if hasattr(timer, "daemon"):
            timer.daemon = True
        with self._lock:
            self._active_timers.append(timer)
        if hasattr(timer, "start"):
            timer.start()
        return timer

    def cancel_all(self) -> None:
        """Cancel all active retry timers."""
        with self._lock:
            for timer in self._active_timers:
                if hasattr(timer, "cancel"):
                    try:
                        timer.cancel()
                    except Exception:
                        pass
            self._active_timers.clear()


default_retry_scheduler = WebhookRetryScheduler()


def _execute_send_attempt(req: urllib.request.Request) -> Dict[str, Any]:
    """Execute a single HTTP POST delivery attempt with 10s timeout without following redirects."""
    try:
        with _open_request(req, timeout=10) as response:
            status_code = response.getcode()
    except urllib.error.HTTPError as exc:
        return {"success": False, "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"success": False, "reason": str(exc)}

    if 200 <= status_code < 300:
        return {"success": True}
    else:
        return {"success": False, "reason": f"HTTP {status_code}"}


def transition_terminal_state(
    *,
    job: Optional[Dict[str, Any]] = None,
    project: Optional[Any] = None,
    job_id: Optional[str] = None,
    new_state: str,
    extra_fields: Optional[Dict[str, Any]] = None,
    notify_url: Optional[str] = None,
    notify_secret_file: Optional[str] = None,
    notify_allow_http: Optional[bool] = None,
) -> Dict[str, Any]:
    """Shared helper for terminal/blocked state transitions (T047, US3, FR-009, FR-010, FR-015).

    Atomically updates the job record: sets state + status, appends an event,
    sends a signed webhook notice when configured, and stores a delivery record.

    Returns the updated job dict.
    """
    from . import records
    from datetime import datetime, timezone
    from pathlib import Path

    timestamp = datetime.now(timezone.utc).isoformat()
    extra = dict(extra_fields) if extra_fields else {}

    if job_id is None and job is not None:
        job_id = job.get("id") or job.get("job_id")

    # Auto-resolve preferences from project if notify settings not explicitly provided
    if project is not None and (notify_url is None or notify_secret_file is None):
        try:
            from . import config
            prefs = config.load_prefs(Path(project))
            if notify_url is None:
                notify_url = getattr(prefs, "notify_url", None)
            if notify_secret_file is None:
                notify_secret_file = getattr(prefs, "notify_secret_file", None)
            if notify_allow_http is None:
                notify_allow_http = getattr(prefs, "notify_allow_http", False)
        except Exception:
            pass

    if notify_allow_http is None:
        notify_allow_http = False

    def apply(stored: dict) -> dict:
        # Check if already transitioned to this terminal state to avoid duplicate notices
        if stored.get("state") == new_state and stored.get("status") == new_state:
            existing_events = stored.get("events", [])
            already_has_event = any(
                isinstance(e, dict)
                and (
                    e.get("type") == new_state
                    or (isinstance(e.get("payload"), dict) and e["payload"].get("type") == new_state)
                )
                for e in existing_events
            )
            if already_has_event:
                return stored

        stored["state"] = new_state
        stored["status"] = new_state

        if extra:
            if "pr_url" in extra:
                stored["pr_url"] = extra["pr_url"]
            if "result" in extra:
                curr_res = stored.get("result")
                if isinstance(curr_res, dict) and isinstance(extra["result"], dict):
                    merged_res = dict(curr_res)
                    merged_res.update(extra["result"])
                    stored["result"] = merged_res
                else:
                    stored["result"] = extra["result"]

        event_payload = {"type": new_state, "timestamp": timestamp}
        event_payload.update(extra)

        try:
            from .interview_service import _reconstruct_event_log
            log = _reconstruct_event_log(stored)
            seq = log.append(event_payload)
        except Exception:
            seq = len(stored.get("events", [])) + 1

        event_entry = {
            "sequence": seq,
            "type": new_state,
            "payload": event_payload,
            **event_payload,
        }
        stored.setdefault("events", []).append(event_entry)

        # Build notice payload
        resolved_job_id = stored.get("id") or stored.get("job_id") or job_id
        resolved_pr_url = stored.get("pr_url") or extra.get("pr_url")
        notice_payload = {
            "job_id": resolved_job_id,
            "status": new_state,
            "pr_url": resolved_pr_url,
            "timestamp": timestamp,
        }
        stored["notice_payload"] = notice_payload

        # Send webhook if configured
        if notify_url and notify_secret_file:
            try:
                result = send_webhook_notice(
                    job_record=notice_payload,
                    notify_url=notify_url,
                    notify_secret_file=notify_secret_file,
                    notify_allow_http=bool(notify_allow_http),
                )
            except Exception as exc:
                result = {"success": False, "reason": str(exc)}
            delivery_record = {
                "status": new_state,
                "timestamp": timestamp,
                "success": result.get("success", False),
                "reason": result.get("reason"),
            }
            stored.setdefault("webhook_deliveries", []).append(delivery_record)

            if not result.get("success", False):
                _persist_pending_notice(
                    stored=stored,
                    notify_url=notify_url,
                    notify_secret_file=notify_secret_file,
                    notify_allow_http=bool(notify_allow_http),
                    payload=notice_payload,
                    timestamp=timestamp,
                )

        return stored

    if job is not None:
        apply(job)
        if project is not None and job_id:
            try:
                records.save_job(project, job)
            except Exception:
                pass
        return job
    elif project is not None and job_id:
        return records.update_job(project, job_id, apply)
    else:
        raise ValueError("Either job or (project and job_id) must be provided")


def send_webhook_notice(
    *,
    job_record: Dict[str, Any],
    notify_url: Optional[str],
    notify_secret_file: str,
    notify_allow_http: bool = False,
    _clock=None,
    _sleeper=None,
    _retry_scheduler: Optional[WebhookRetryScheduler] = None,
) -> Dict[str, Any]:
    """POST webhook notice on terminal or blocked states with exponential backoff retries (FR-017).

    Returns a dict with ``success`` (bool) and optional ``reason`` (str).
    """
    if not notify_url:
        return {"success": False, "reason": "notify_url not configured"}

    # Enforce HTTPS unless explicitly allowed
    if not notify_allow_http and not notify_url.startswith("https://"):
        return {"success": False, "reason": "HTTPS required for outbound webhooks"}

    # Check secret file existence and permissions (must be regular file with 0600 permissions on POSIX, FR-016)
    try:
        st = os.stat(notify_secret_file)
    except OSError:
        return {"success": False, "reason": f"cannot read secret file: {notify_secret_file}"}

    if not stat.S_ISREG(st.st_mode):
        return {"success": False, "reason": f"secret file must be a regular file: {notify_secret_file}"}

    # On POSIX, require 0600 permissions (FR-016).
    # On Windows (nt), chmod has no effect on mode bits, so the mode-bit gate is not enforced;
    # access control relies on Windows filesystem ACLs (FR-016).
    if os.name != "nt":
        file_mode = stat.S_IMODE(st.st_mode)
        if file_mode != 0o600:
            return {
                "success": False,
                "reason": f"notify_secret_file must have 0600 permissions (got {oct(file_mode)})",
            }

    # Read secret from file (do not log secret or signature)
    try:
        with open(notify_secret_file, "rb") as f:
            secret = f.read()
    except OSError:
        return {"success": False, "reason": f"cannot read secret file: {notify_secret_file}"}

    # Build payload (do not mutate caller's job_record)
    payload = {
        "job_id": job_record.get("job_id"),
        "status": job_record.get("status"),
        "pr_url": job_record.get("pr_url"),
        "timestamp": job_record.get("timestamp"),
    }
    body = json.dumps(payload)

    # Compute signature
    signature = _compute_signature(secret, body)

    # Send POST using urllib (standard library only, FR-002)
    req = urllib.request.Request(
        notify_url,
        data=body.encode("utf-8"),
        headers={"X-ACC-Signature": signature},
        method="POST",
    )

    # If an explicit _sleeper test double is injected, execute synchronous test loop
    if _sleeper is not None:
        last_reason = None
        for attempt in range(1 + _MAX_RETRIES):
            result = _execute_send_attempt(req)
            if result["success"]:
                return result
            last_reason = result["reason"]
            if attempt < _MAX_RETRIES:
                _sleeper(_BACKOFF_SCHEDULE[attempt])
        return {"success": False, "reason": last_reason or "unknown error"}

    # Initial attempt (non-blocking for retries)
    initial_result = _execute_send_attempt(req)
    if initial_result["success"]:
        return initial_result

    # Schedule asynchronous retries via WebhookRetryScheduler
    scheduler = _retry_scheduler or default_retry_scheduler

    def _make_retry_action(retry_idx: int):
        def _retry():
            res = _execute_send_attempt(req)
            if not res["success"]:
                next_idx = retry_idx + 1
                if next_idx < _MAX_RETRIES:
                    scheduler.schedule_retry(next_idx, _make_retry_action(next_idx))
        return _retry

    scheduler.schedule_retry(0, _make_retry_action(0))
    return initial_result


def _persist_pending_notice(
    *,
    stored: Dict[str, Any],
    notify_url: str,
    notify_secret_file: str,
    notify_allow_http: bool,
    payload: Dict[str, Any],
    timestamp: str,
) -> Dict[str, Any]:
    """Persist a bounded pending notice in the job record on first delivery failure (FR-017)."""
    import uuid
    from datetime import datetime, timezone, timedelta

    notice_id = f"notice-{uuid.uuid4().hex[:12]}"
    backoff_env = os.environ.get("ACC_WEBHOOK_BACKOFF")
    if backoff_env is not None:
        try:
            delay_s = int(backoff_env)
        except ValueError:
            delay_s = _BACKOFF_SCHEDULE[0]
    else:
        delay_s = _BACKOFF_SCHEDULE[0]

    now_dt = datetime.now(timezone.utc)
    next_due_dt = now_dt + timedelta(seconds=delay_s)
    expires_at_dt = now_dt + timedelta(days=1)

    pending_notice = {
        "id": notice_id,
        "notify_url": notify_url,
        "notify_secret_file": notify_secret_file,
        "notify_allow_http": bool(notify_allow_http),
        "payload": dict(payload),
        "attempts": 1,
        "max_attempts": _MAX_RETRIES,
        "next_due": next_due_dt.isoformat(),
        "expires_at": expires_at_dt.isoformat(),
        "delivered": False,
        "created_at": timestamp,
    }

    pending_list = stored.setdefault("pending_notices", [])
    pending_list.append(pending_notice)
    if len(pending_list) > 5:
        stored["pending_notices"] = pending_list[-5:]
    stored["pending_notice"] = pending_notice
    return pending_notice


def _send_single_notice_attempt(notice: Dict[str, Any]) -> Dict[str, Any]:
    """Send a single signed HTTP delivery attempt for a pending notice without redirects."""
    notify_url = notice.get("notify_url")
    notify_secret_file = notice.get("notify_secret_file")
    notify_allow_http = notice.get("notify_allow_http", False)
    payload = notice.get("payload", {})

    if not notify_url:
        return {"success": False, "reason": "notify_url not configured"}

    if not notify_allow_http and not notify_url.startswith("https://"):
        return {"success": False, "reason": "HTTPS required for outbound webhooks"}

    try:
        st = os.stat(notify_secret_file)
    except OSError:
        return {"success": False, "reason": f"cannot read secret file: {notify_secret_file}"}

    if not stat.S_ISREG(st.st_mode):
        return {"success": False, "reason": f"secret file must be a regular file: {notify_secret_file}"}

    if os.name != "nt":
        file_mode = stat.S_IMODE(st.st_mode)
        if file_mode != 0o600:
            return {
                "success": False,
                "reason": f"notify_secret_file must have 0600 permissions (got {oct(file_mode)})",
            }

    try:
        with open(notify_secret_file, "rb") as f:
            secret = f.read()
    except OSError:
        return {"success": False, "reason": f"cannot read secret file: {notify_secret_file}"}

    body = json.dumps(payload)
    signature = _compute_signature(secret, body)

    req = urllib.request.Request(
        notify_url,
        data=body.encode("utf-8"),
        headers={"X-ACC-Signature": signature},
        method="POST",
    )
    return _execute_send_attempt(req)


def drain_pending_notices(
    project: Any,
    job_id: Optional[str] = None,
    *,
    job: Optional[Dict[str, Any]] = None,
    job_ids: Optional[List[str]] = None,
    clock: Optional[Any] = None,
) -> Any:
    """Drain due pending webhook notices for a job or jobs in a project.

    Sends due notices with valid HMAC signatures computed at send time from the
    secret file, records deliveries, marks delivered notices so they are never
    sent again, and applies exponential backoff on retry failure.
    """
    from datetime import datetime, timezone, timedelta
    from pathlib import Path
    from . import records

    proj_path = Path(project)

    if clock is not None and hasattr(clock, "time"):
        now_dt = datetime.fromtimestamp(clock.time(), tz=timezone.utc)
    else:
        now_dt = datetime.now(timezone.utc)

    def _drain_job_dict(target_job: Dict[str, Any]) -> bool:
        modified = False
        pending_list = target_job.get("pending_notices")
        if not pending_list and target_job.get("pending_notice"):
            pending_list = [target_job["pending_notice"]]

        if not pending_list:
            return False

        for notice in pending_list:
            if not isinstance(notice, dict):
                continue
            if notice.get("delivered"):
                continue

            # Check expiration
            expires_at = notice.get("expires_at")
            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(expires_at)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if now_dt > exp_dt:
                        continue
                except Exception:
                    pass

            # Check max attempts
            attempts = notice.get("attempts", 0)
            max_attempts = notice.get("max_attempts", _MAX_RETRIES)
            if attempts >= max_attempts:
                continue

            # Check next_due
            next_due = notice.get("next_due")
            if next_due:
                try:
                    due_dt = datetime.fromisoformat(next_due)
                    if due_dt.tzinfo is None:
                        due_dt = due_dt.replace(tzinfo=timezone.utc)
                    if now_dt < due_dt:
                        continue
                except Exception:
                    pass

            # Notice is due! Attempt delivery
            result = _send_single_notice_attempt(notice)
            timestamp = now_dt.isoformat()
            success = result.get("success", False)

            if success:
                notice["delivered"] = True
                notice["delivered_at"] = timestamp
                if (
                    isinstance(target_job.get("pending_notice"), dict)
                    and target_job["pending_notice"].get("id") == notice.get("id")
                ):
                    target_job["pending_notice"]["delivered"] = True
                    target_job["pending_notice"]["delivered_at"] = timestamp

                delivery_record = {
                    "status": notice.get("payload", {}).get("status"),
                    "timestamp": timestamp,
                    "success": True,
                    "reason": None,
                    "notice_id": notice.get("id"),
                }
                target_job.setdefault("webhook_deliveries", []).append(delivery_record)
                modified = True
            else:
                attempts += 1
                notice["attempts"] = attempts
                attempt_idx = attempts - 1
                backoff_env = os.environ.get("ACC_WEBHOOK_BACKOFF")
                if backoff_env is not None:
                    try:
                        delay_s = int(backoff_env)
                    except ValueError:
                        delay_s = _BACKOFF_SCHEDULE[min(attempt_idx, len(_BACKOFF_SCHEDULE) - 1)]
                else:
                    delay_s = _BACKOFF_SCHEDULE[min(attempt_idx, len(_BACKOFF_SCHEDULE) - 1)]

                if attempts < max_attempts:
                    notice["next_due"] = (now_dt + timedelta(seconds=delay_s)).isoformat()
                else:
                    notice["next_due"] = None

                delivery_record = {
                    "status": notice.get("payload", {}).get("status"),
                    "timestamp": timestamp,
                    "success": False,
                    "reason": result.get("reason"),
                    "notice_id": notice.get("id"),
                    "attempt": attempts,
                }
                target_job.setdefault("webhook_deliveries", []).append(delivery_record)
                modified = True

        return modified

    if job is not None:
        jid = job_id or job.get("id") or job.get("job_id")
        pending = job.get("pending_notices") or ([job["pending_notice"]] if job.get("pending_notice") else [])
        has_due = False
        for n in pending:
            if isinstance(n, dict) and not n.get("delivered"):
                due_val = n.get("next_due")
                if not due_val:
                    has_due = True
                    break
                try:
                    d_dt = datetime.fromisoformat(due_val)
                    if d_dt.tzinfo is None:
                        d_dt = d_dt.replace(tzinfo=timezone.utc)
                    if now_dt >= d_dt:
                        has_due = True
                        break
                except Exception:
                    has_due = True
                    break

        if not has_due:
            return job

        if jid:
            with records.job_lock(proj_path, jid):
                modified = _drain_job_dict(job)
                if modified:
                    try:
                        records.save_job(proj_path, job)
                    except Exception:
                        pass
        else:
            _drain_job_dict(job)
        return job

    if job_ids:
        targets = list(job_ids)
    elif job_id:
        targets = [job_id]
    else:
        targets = []
        try:
            for j, _, err in records.iter_jobs(proj_path):
                if j is not None and not err:
                    if j.get("pending_notices") or j.get("pending_notice"):
                        targets.append(j.get("id"))
        except Exception:
            targets = []

    results = []
    for tid in targets:
        if not tid:
            continue
        try:
            with records.job_lock(proj_path, tid):
                loaded_job = records.load_job(proj_path, tid, _drain=False)
                if _drain_job_dict(loaded_job):
                    records.save_job(proj_path, loaded_job)
                results.append(loaded_job)
        except Exception:
            pass

    return results


drain = drain_pending_notices
drain_pending_webhook_notices = drain_pending_notices

