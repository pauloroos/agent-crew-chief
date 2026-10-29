from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path

import pytest

from crew_chief import update_cache as cache

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _reserve(root, queue):
    queue.put(cache.reserve_check(Path(root), NOW))


def test_reservation_throttles_failure_and_crash(tmp_path):
    assert cache.reserve_check(tmp_path, NOW)
    assert not cache.reserve_check(tmp_path, NOW + timedelta(hours=23))
    assert cache.cached_notice(tmp_path, '0.4.3', True, NOW) is None
    assert cache.reserve_check(tmp_path, NOW + timedelta(days=1))


def test_notice_exact_and_expires(tmp_path):
    assert cache.reserve_check(tmp_path, NOW)
    cache.store_result(tmp_path, NOW.isoformat(), 'v0.4.10')
    expected = ('ACC v0.4.10 available. Update manually in your ACC source clone: '
                'git fetch --tags; git checkout v0.4.10; python -I install.py --command; '
                'python -I install.py <project>')
    assert cache.cached_notice(tmp_path, '0.4.3', True, NOW) == expected
    assert cache.cached_notice(tmp_path, '0.4.3', False, NOW) is None
    assert cache.cached_notice(tmp_path, '0.4.10', True, NOW) is None
    assert cache.cached_notice(tmp_path, '0.5.0', True, NOW) is None
    assert cache.cached_notice(tmp_path, '0.4.3', True, NOW + timedelta(days=1)) is None
    assert not cache.reserve_check(tmp_path, NOW - timedelta(seconds=1))
    assert cache.cached_notice(tmp_path, '0.4.3', True, NOW - timedelta(seconds=1)) is None
    cache.store_result(tmp_path, NOW.isoformat(), None)
    assert cache.cached_notice(tmp_path, '0.4.3', True, NOW) is None


@pytest.mark.parametrize('payload', ['broken', '[]', '{}', 'x'*9000,
    '{"schema_version":1,"checked_at":true}',
    '{"schema_version":true,"checked_at":"2026-01-01T00:00:00+00:00"}'])
def test_corruption_is_silent_and_replaceable(tmp_path, payload):
    (tmp_path/'update-check.json').write_text(payload)
    assert cache.cached_notice(tmp_path, '0.4.3', True, NOW) is None
    assert cache.reserve_check(tmp_path, NOW)


def test_bom_crlf_and_minimal_schema(tmp_path):
    cache.reserve_check(tmp_path, NOW)
    cache.store_result(tmp_path, NOW.isoformat(), 'v0.4.4')
    path = tmp_path/'update-check.json'
    data = json.loads(path.read_text())
    assert set(data) == {'schema_version','checked_at','successful_at','latest_version'}
    path.write_bytes(b'\xef\xbb\xbf'+json.dumps(data, indent=2).replace('\n','\r\n').encode())
    assert cache.cached_notice(tmp_path, '0.4.3', True, NOW)


@pytest.mark.parametrize('latest', ['v1.2.3\x1b[0m', '1.2.3;echo hi', 'v1.2.3-rc1', True, 'v01.2.3'])
def test_untrusted_version_never_reaches_notice(tmp_path, latest):
    cache.reserve_check(tmp_path, NOW)
    cache.store_result(tmp_path, NOW.isoformat(), latest)
    assert cache.cached_notice(tmp_path, '0.4.3', True, NOW) is None


def test_nonwaiting_lock_and_unwritable_cache(tmp_path, monkeypatch):
    lock = tmp_path/'update-check.lock'
    lock.write_text('uncertain owner')
    assert not cache.reserve_check(tmp_path, NOW)
    assert lock.read_text() == 'uncertain owner'
    lock.unlink()
    monkeypatch.setattr(cache.os, 'replace', lambda *a: (_ for _ in ()).throw(PermissionError()))
    assert not cache.reserve_check(tmp_path, NOW)
    assert not (tmp_path/'update-check.json').exists()
    assert list(tmp_path.iterdir()) == []


def test_failed_atomic_replace_preserves_old_record(tmp_path, monkeypatch):
    cache.reserve_check(tmp_path, NOW)
    path = tmp_path/'update-check.json'
    before = path.read_bytes()
    monkeypatch.setattr(cache.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError()))
    cache.store_result(tmp_path, NOW.isoformat(), 'v0.4.4')
    assert path.read_bytes() == before


def test_late_result_cannot_overwrite_new_reservation(tmp_path):
    cache.reserve_check(tmp_path, NOW)
    cache.reserve_check(tmp_path, NOW + timedelta(days=1))
    before = (tmp_path/'update-check.json').read_bytes()
    cache.store_result(tmp_path, NOW.isoformat(), 'v9.0.0')
    assert (tmp_path/'update-check.json').read_bytes() == before


def test_processes_reserve_once(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    queue = ctx.Queue()
    children = [ctx.Process(target=_reserve, args=(str(tmp_path), queue)) for _ in range(4)]
    try:
        for child in children:
            child.start()
        assert sum(queue.get(timeout=15) for _ in children) == 1
    finally:
        for child in children:
            if child.pid is not None:
                child.join(15)
                if child.is_alive():
                    child.terminate()
                    child.join(5)
        queue.close()
        queue.join_thread()
    assert all(child.exitcode == 0 for child in children)
