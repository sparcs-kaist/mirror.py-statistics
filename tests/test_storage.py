"""Unit tests for mirror_plugin_statistics.storage."""

from __future__ import annotations

import time
from pathlib import Path

from mirror_plugin_statistics.storage import (
    Sample,
    get_all_latest,
    get_history,
    get_latest,
    init_db,
    insert_sample,
    prune,
)


def _make_sample(pkgid: str, ts: float, bytes_: int, source: str = "du") -> Sample:
    """Build a Sample for tests."""
    return Sample(pkgid=pkgid, ts=ts, bytes=bytes_, file_count=None, source=source)


def test_init_db_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    init_db(db_path)
    assert db_path.exists()
    # Idempotent: calling again must not raise.
    init_db(db_path)


def test_insert_and_get_latest(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    now = time.time()
    insert_sample(db_path, _make_sample("pkg1", now - 10, 100))
    insert_sample(db_path, _make_sample("pkg1", now, 200))

    latest = get_latest(db_path, "pkg1")
    assert latest is not None
    assert latest.pkgid == "pkg1"
    assert latest.bytes == 200
    assert latest.ts == now

    assert get_latest(db_path, "missing") is None


def test_get_all_latest(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    now = time.time()
    insert_sample(db_path, _make_sample("pkg1", now - 10, 100))
    insert_sample(db_path, _make_sample("pkg1", now, 150))
    insert_sample(db_path, _make_sample("pkg2", now - 5, 300))

    latest = get_all_latest(db_path)
    assert set(latest.keys()) == {"pkg1", "pkg2"}
    assert latest["pkg1"].bytes == 150
    assert latest["pkg2"].bytes == 300


def test_get_history_since_and_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    base = time.time()
    for i in range(5):
        insert_sample(db_path, _make_sample("pkg1", base + i, i * 10))

    all_history = get_history(db_path, "pkg1")
    assert [s.bytes for s in all_history] == [0, 10, 20, 30, 40]
    # Ascending time order.
    assert all(all_history[i].ts <= all_history[i + 1].ts for i in range(len(all_history) - 1))

    since_history = get_history(db_path, "pkg1", since_ts=base + 2)
    assert [s.bytes for s in since_history] == [20, 30, 40]

    limited_history = get_history(db_path, "pkg1", limit=2)
    assert [s.bytes for s in limited_history] == [30, 40]
    assert limited_history[0].ts <= limited_history[1].ts

    other_pkg_history = get_history(db_path, "missing")
    assert other_pkg_history == []


def test_prune_removes_only_old_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    now = time.time()
    old_ts = now - 400 * 86400
    insert_sample(db_path, _make_sample("pkg1", old_ts, 100))
    insert_sample(db_path, _make_sample("pkg1", now, 200))

    deleted = prune(db_path, retention_days=365)
    assert deleted == 1

    remaining = get_history(db_path, "pkg1")
    assert len(remaining) == 1
    assert remaining[0].bytes == 200


def test_prune_noop_when_retention_non_positive(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    insert_sample(db_path, _make_sample("pkg1", time.time() - 1000 * 86400, 100))

    assert prune(db_path, retention_days=0) == 0
    assert prune(db_path, retention_days=-1) == 0
    assert len(get_history(db_path, "pkg1")) == 1
