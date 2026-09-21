"""Integration tests wiring the plug-in, worker, storage, and mirror.py hooks
together.

These tests never touch a live daemon: the POST event is exercised through
mirror.event/mirror.plugin registration (in-process), and measurement is
driven directly through the worker (either synchronously via ``_process`` or
via ``enqueue`` polled with a timeout), never through a real ``mirror`` sync.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest

import mirror.config
import mirror.event
import mirror.plugin

import mirror_plugin_statistics as statistics_plugin
from conftest import make_package
from mirror_plugin_statistics import config as config_module
from mirror_plugin_statistics import storage
from mirror_plugin_statistics import worker as worker_module
from mirror_plugin_statistics.config import StatisticsConfig
from mirror_plugin_statistics.measure.providers import MeasureResult
from mirror_plugin_statistics.storage import Sample, get_latest
from mirror_plugin_statistics.util import format_bytes

EVENT_NAME = "MASTER.PACKAGE_STATUS_UPDATE.POST"
INIT_EVENT_NAME = "MASTER.INIT.POST"


@pytest.fixture(autouse=True)
def _reset_worker_singleton():
    """Reset the module-level worker singleton around each test.

    Guarantees each test that touches worker.get_worker() starts from a clean
    (unstarted) singleton, and that a monkeypatched config_loader from one
    test never leaks into the next.
    """
    worker_module._worker = None
    yield
    worker_module._worker = None


# ---------------------------------------------------------------------------
# setup() registers the POST listener
# ---------------------------------------------------------------------------

def test_setup_registers_post_listener() -> None:
    record = statistics_plugin.plugin()
    mirror.plugin._register_status(record)

    before = mirror.event._manager._listeners.get(EVENT_NAME, [])
    assert not any(cb is statistics_plugin._on_status for _, cb in before)

    try:
        record.setup()

        after = mirror.event._manager._listeners.get(EVENT_NAME, [])
        assert any(cb is statistics_plugin._on_status for _, cb in after)
    finally:
        mirror.event.off(EVENT_NAME, statistics_plugin._on_status)
        mirror.event.off(INIT_EVENT_NAME, statistics_plugin._on_init)


# ---------------------------------------------------------------------------
# _on_status guard logic
# ---------------------------------------------------------------------------

def test_on_status_enqueues_only_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class _FakeWorker:
        def enqueue(self, pkgid: str, dst: str) -> None:
            calls.append((pkgid, dst))

    monkeypatch.setattr(statistics_plugin.worker, "get_worker", lambda: _FakeWorker())

    package = make_package(pkgid="pkg1", dst="/srv/ftp/pkg1", status="SYNC")
    statistics_plugin._on_status(package, "SYNC")
    assert calls == []

    statistics_plugin._on_status(package, "ACTIVE")
    assert calls == [("pkg1", "/srv/ftp/pkg1")]

    package_no_dst = make_package(pkgid="pkg2", dst="", status="ACTIVE")
    statistics_plugin._on_status(package_no_dst, "ACTIVE")
    assert calls == [("pkg1", "/srv/ftp/pkg1")]


# ---------------------------------------------------------------------------
# Measurement path: enqueue -> worker thread -> storage -> web status hook
# ---------------------------------------------------------------------------

def test_enqueue_measures_and_extend_web_status_fields_returns_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dst_dir = tmp_path / "repo"
    dst_dir.mkdir()
    (dst_dir / "payload.bin").write_bytes(b"x" * 8192)

    config = StatisticsConfig(data_dir=tmp_path / "data", providers=["du"])
    monkeypatch.setattr(worker_module, "load_config", lambda: config)

    package = make_package(pkgid="pkg1", dst=str(dst_dir), status="ACTIVE")

    w = worker_module.get_worker()
    w.enqueue(package.pkgid, package.settings.dst)

    deadline = time.time() + 5
    sample = None
    while time.time() < deadline:
        sample = w.get_cached(package.pkgid)
        if sample is not None:
            break
        time.sleep(0.05)

    assert sample is not None, "measurement did not complete within timeout"
    assert sample.bytes > 0

    stored = get_latest(config.db_path(), package.pkgid)
    assert stored is not None
    assert stored.bytes == sample.bytes
    assert stored.source == sample.source

    fields = statistics_plugin.extend_web_status_fields(package)
    assert fields is not None
    assert fields["bytes"] == sample.bytes
    assert fields["human"] == format_bytes(sample.bytes)
    assert fields["measured_at"] == sample.ts
    assert fields["source"] == sample.source


def test_extend_web_status_fields_none_when_never_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = StatisticsConfig(data_dir=tmp_path / "data", providers=["du"])
    monkeypatch.setattr(worker_module, "load_config", lambda: config)

    package = make_package(pkgid="never-measured", dst="/srv/ftp/never-measured")

    assert statistics_plugin.extend_web_status_fields(package) is None


@pytest.mark.parametrize("has_old_sample", [False, True])
def test_cold_cache_read_preserves_concurrent_measurement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, has_old_sample: bool
) -> None:
    config = StatisticsConfig(data_dir=tmp_path / "data", providers=["du"])
    pkgid = "concurrent-repo"
    if has_old_sample:
        storage.insert_sample(
            config.db_path(),
            Sample(
                pkgid=pkgid, ts=time.time() - 60, bytes=100,
                file_count=None, source="du",
            ),
        )

    read_finished = Event()
    measurement_finished = Event()

    def read_delayed_sample(db_path: Path, repo_id: str) -> Sample | None:
        sample = get_latest(db_path, repo_id)
        read_finished.set()
        assert measurement_finished.wait(timeout=5)
        return sample

    monkeypatch.setattr(storage, "get_latest", read_delayed_sample)
    monkeypatch.setattr(
        worker_module, "measure_usage",
        lambda dst, providers: MeasureResult(bytes=200, file_count=None, source="du"),
    )
    monkeypatch.setattr(mirror.config, "generate_and_save_web_status", lambda: None)
    worker = worker_module.MeasureWorker(config_loader=lambda: config)

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending_read = executor.submit(worker.get_cached, pkgid)
        try:
            assert read_finished.wait(timeout=5)
            worker._process(pkgid, str(tmp_path / "repo"), config)
        finally:
            measurement_finished.set()
        returned = pending_read.result(timeout=5)

    latest = get_latest(config.db_path(), pkgid)
    assert latest is not None
    assert latest.bytes == 200
    assert returned == latest
    assert worker.get_cached(pkgid) == latest


# ---------------------------------------------------------------------------
# Re-emit: web status is refreshed after the sample lands in storage
# ---------------------------------------------------------------------------

def test_process_refreshes_web_status_after_sample_insert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dst_dir = tmp_path / "repo"
    dst_dir.mkdir()
    (dst_dir / "blob.bin").write_bytes(b"y" * 4096)

    config = StatisticsConfig(data_dir=tmp_path / "data", providers=["du"])
    pkgid = "pkg-reemit"

    seen_at_refresh_time: dict[str, object] = {}

    def _spy_generate_and_save_web_status() -> None:
        seen_at_refresh_time["latest"] = get_latest(config.db_path(), pkgid)

    spy = MagicMock(side_effect=_spy_generate_and_save_web_status)
    monkeypatch.setattr(mirror.config, "generate_and_save_web_status", spy)

    w = worker_module.MeasureWorker(config_loader=lambda: config)
    w._process(pkgid, str(dst_dir), config)

    spy.assert_called_once()
    assert seen_at_refresh_time["latest"] is not None
    assert seen_at_refresh_time["latest"].bytes > 0


# ---------------------------------------------------------------------------
# setup() registers the INIT listener (startup backfill)
# ---------------------------------------------------------------------------

def test_setup_registers_init_listener() -> None:
    record = statistics_plugin.plugin()
    mirror.plugin._register_status(record)

    before = mirror.event._manager._listeners.get(INIT_EVENT_NAME, [])
    assert not any(cb is statistics_plugin._on_init for _, cb in before)

    try:
        record.setup()

        after = mirror.event._manager._listeners.get(INIT_EVENT_NAME, [])
        assert any(cb is statistics_plugin._on_init for _, cb in after)

        post_after = mirror.event._manager._listeners.get(EVENT_NAME, [])
        assert any(cb is statistics_plugin._on_status for _, cb in post_after)
    finally:
        mirror.event.off(INIT_EVENT_NAME, statistics_plugin._on_init)
        mirror.event.off(EVENT_NAME, statistics_plugin._on_status)


# ---------------------------------------------------------------------------
# _on_init: startup backfill for repos with no recorded sample
# ---------------------------------------------------------------------------

class _FakePackages:
    """Minimal mirror.packages stand-in exposing only .keys() and .get()."""

    def __init__(self, packages: dict) -> None:
        self._packages = packages

    def keys(self) -> list:
        return list(self._packages.keys())

    def get(self, pkgid: str):
        return self._packages.get(pkgid)


def test_on_init_backfills_repos_without_existing_sample(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = StatisticsConfig(data_dir=tmp_path / "data", providers=["du"])
    monkeypatch.setattr(config_module, "load_config", lambda: config)

    already_dst = tmp_path / "already-repo"
    already_dst.mkdir()
    missing_dst = tmp_path / "missing-repo"
    missing_dst.mkdir()

    already_pkg = make_package(pkgid="already", dst=str(already_dst))
    missing_pkg = make_package(pkgid="missing", dst=str(missing_dst))

    storage.insert_sample(
        config.db_path(),
        Sample(pkgid="already", ts=time.time(), bytes=123, file_count=1, source="du"),
    )

    monkeypatch.setattr(
        mirror,
        "packages",
        _FakePackages({"already": already_pkg, "missing": missing_pkg}),
        raising=False,
    )

    calls: list[tuple[str, str]] = []
    w = worker_module.get_worker()
    monkeypatch.setattr(w, "enqueue", lambda pkgid, dst: calls.append((pkgid, dst)))

    statistics_plugin._on_init()

    assert calls == [("missing", str(missing_dst))]


def test_on_init_skips_disabled_and_empty_dst_packages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = StatisticsConfig(data_dir=tmp_path / "data", providers=["du"])
    monkeypatch.setattr(config_module, "load_config", lambda: config)

    disabled_dst = tmp_path / "disabled-repo"
    disabled_dst.mkdir()
    disabled_pkg = make_package(pkgid="disabled", dst=str(disabled_dst))
    disabled_pkg.disabled = True

    empty_dst_pkg = make_package(pkgid="empty-dst", dst="")

    monkeypatch.setattr(
        mirror,
        "packages",
        _FakePackages({"disabled": disabled_pkg, "empty-dst": empty_dst_pkg}),
        raising=False,
    )

    calls: list[tuple[str, str]] = []
    w = worker_module.get_worker()
    monkeypatch.setattr(w, "enqueue", lambda pkgid, dst: calls.append((pkgid, dst)))

    statistics_plugin._on_init()

    assert calls == []
