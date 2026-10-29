"""Small, best-effort daily update cache; never waits for another writer."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile

DAY = timedelta(days=1)
_VERSION = re.compile(r'v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})', re.ASCII)


def version_tuple(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def format_notice(latest: object, current: object) -> str | None:
    """Only locally constructed commands with strictly validated versions."""
    new, old = version_tuple(latest), version_tuple(current)
    if new is None or old is None or new <= old:
        return None
    version = 'v' + '.'.join(map(str, new))
    return (f'ACC {version} available. Update manually in your ACC source clone: '
            f'git fetch --tags; git checkout {version}; python -I install.py --command; '
            'python -I install.py <project>')


def _time(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _read(config_dir: Path) -> dict | None:
    try:
        with (config_dir / 'update-check.json').open('rb') as handle:
            raw = handle.read(8193)
        if len(raw) > 8192:
            return None
        data = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(data, dict) or type(data.get('schema_version')) is not int
                or data['schema_version'] != 1 or _time(data.get('checked_at')) is None):
            return None
        return data
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


@contextmanager
def _transaction(config_dir: Path):
    lock = config_dir / 'update-check.lock'
    acquired = False
    try:
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            acquired = True
            os.close(fd)
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            try:
                lock.unlink()
            except OSError:
                pass


def _write(config_dir: Path, data: dict) -> bool:
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix='.update-', dir=config_dir)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(data, handle, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, config_dir / 'update-check.json')
        return True
    except OSError:
        return False
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass


def reserve_check(config_dir: Path, now: datetime) -> bool:
    """Persist the attempt before HTTP, including failed/crashed attempts."""
    stamp = _time(now)
    if stamp is None:
        return False
    with _transaction(config_dir) as acquired:
        if not acquired:
            return False
        old = _read(config_dir)
        if old is not None and stamp - _time(old['checked_at']) < DAY:
            return False
        return _write(config_dir, dict(schema_version=1, checked_at=stamp.isoformat(),
                                       latest_version=None, successful_at=None))


def store_result(config_dir: Path, checked_at: str, latest: str | None) -> None:
    stamp = _time(checked_at)
    if stamp is None:
        return
    with _transaction(config_dir) as acquired:
        if not acquired:
            return
        old = _read(config_dir)
        if old is None or _time(old['checked_at']) != stamp:
            return
        version = version_tuple(latest)
        canonical = 'v' + '.'.join(map(str, version)) if version else None
        _write(config_dir, dict(schema_version=1, checked_at=stamp.isoformat(),
                               latest_version=canonical,
                               successful_at=stamp.isoformat() if canonical else None))


def check_due(config_dir: Path, now: datetime) -> bool:
    """Read-only: whether a new update check attempt is due now.

    Absent or corrupt cache counts as due; a ``checked_at`` in the future or
    less than 24 hours ago counts as not due. Never locks or writes.
    """
    stamp = _time(now)
    if stamp is None:
        return False
    data = _read(Path(config_dir))
    if data is None:
        return True
    checked = _time(data.get('checked_at'))
    if checked is None:
        return True
    if checked > stamp:
        return False
    return stamp - checked >= DAY


def cached_notice(config_dir: Path, current: str, enabled: bool,
                  now: datetime) -> str | None:
    if enabled is not True:
        return None
    stamp, data = _time(now), _read(config_dir)
    if stamp is None or data is None:
        return None
    checked, success = _time(data['checked_at']), _time(data.get('successful_at'))
    if success is None or success != checked or not timedelta(0) <= stamp-success < DAY:
        return None
    return format_notice(data.get('latest_version'), current)
