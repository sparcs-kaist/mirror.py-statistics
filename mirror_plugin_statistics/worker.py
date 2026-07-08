"""Background measurement worker.

A single daemon thread drains a queue of (pkgid, dst) jobs. Keeping measurement
off mirror.py's event thread pool (and off the synchronous status-write path)
means a slow `du` on a multi-million-file mirror never stalls the daemon.

The thread is LAZY-STARTED on the first enqueued job — never in setup() — so
that loading the plug-in in non-daemon contexts (external-plugin registration,
the `mirror plugin config create` CLI) does not spin up a thread. Start is
idempotent and the thread is daemon=True so it never blocks process exit.

After recording a sample the worker also refreshes the web status.json (so the
new size shows up promptly rather than on the next status change) and runs the
configured file exporters.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from . import storage
from .config import StatisticsConfig, load_config
from .exporters import run_exporters
from .measure import measure_usage
from .storage import Sample

log = logging.getLogger("mirror")


class MeasureWorker:
    """Owns the measurement queue, worker thread, and latest-sample cache.

    Args:
        config_loader(Callable[[], StatisticsConfig]): Called (fresh each job)
            to obtain current config, so operator edits take effect without a
            restart. Injected for testability.
    """

    def __init__(self, config_loader: Callable[[], StatisticsConfig]) -> None:
        self._config_loader = config_loader
        self._queue: list[tuple[str, str]] = []
        self._pending: set[str] = set()
        self._cache: dict[str, Sample] = {}
        self._cache_lock = threading.Lock()
        self._queue_cond = threading.Condition()
        self._start_lock = threading.Lock()
        self._started = False
        self._thread: Optional[threading.Thread] = None

    def enqueue(self, pkgid: str, dst: str) -> None:
        """Queue a measurement job, deduping by pkgid, and lazy-start the thread.

        Skips enqueueing when the cached sample for pkgid is newer than
        config.min_interval_seconds (0 disables this throttle).

        Args:
            pkgid(str): Repository id.
            dst(str): Repository on-disk path (package.settings.dst).
        """
        config = self._config_loader()
        if config.min_interval_seconds > 0:
            cached = self.get_cached(pkgid)
            if cached is not None and (time.time() - cached.ts) < config.min_interval_seconds:
                log.debug(
                    "Skipping statistics measurement for %r: last sample is %.1fs old "
                    "(min_interval_seconds=%d)",
                    pkgid, time.time() - cached.ts, config.min_interval_seconds,
                )
                return

        with self._queue_cond:
            if pkgid in self._pending:
                for index, (existing_pkgid, _existing_dst) in enumerate(self._queue):
                    if existing_pkgid == pkgid:
                        self._queue[index] = (pkgid, dst)
                        break
            else:
                self._queue.append((pkgid, dst))
                self._pending.add(pkgid)
            self._queue_cond.notify()

        self._ensure_started()

    def _ensure_started(self) -> None:
        """Start the daemon worker thread once (idempotent)."""
        with self._start_lock:
            if self._started:
                return
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self._started = True

    def _run(self) -> None:
        """Worker loop: pop a job, measure, persist, refresh status, export."""
        while True:
            with self._queue_cond:
                while not self._queue:
                    self._queue_cond.wait()
                pkgid, dst = self._queue.pop(0)
                self._pending.discard(pkgid)

            try:
                config = self._config_loader()
                self._process(pkgid, dst, config)
            except Exception as exc:
                log.warning("Statistics worker failed to process %r: %s", pkgid, exc)

    def _process(self, pkgid: str, dst: str, config: StatisticsConfig) -> None:
        """Measure one repo and record it: measure -> insert -> prune -> cache
        -> refresh web status -> run exporters. Isolates and logs failures.

        Args:
            pkgid(str): Repository id.
            dst(str): Repository on-disk path.
            config(StatisticsConfig): Config snapshot for this job.
        """
        result = measure_usage(dst, config.providers)
        if result is None:
            log.warning("Could not measure disk usage for %r at %r", pkgid, dst)
            return

        sample = Sample(
            pkgid=pkgid,
            ts=time.time(),
            bytes=result.bytes,
            file_count=result.file_count,
            source=result.source,
        )

        db_path = config.db_path()
        storage.init_db(db_path)
        storage.insert_sample(db_path, sample)
        storage.prune(db_path, config.retention_days)

        with self._cache_lock:
            self._cache[pkgid] = sample

        try:
            import mirror.config

            mirror.config.generate_and_save_web_status()
        except Exception as exc:
            log.warning("Failed to refresh web status after measuring %r: %s", pkgid, exc)

        try:
            run_exporters(db_path, config.enabled_exporters())
        except Exception as exc:
            log.warning("Statistics exporters failed for %r: %s", pkgid, exc)

    def get_cached(self, pkgid: str) -> Optional[Sample]:
        """Return the last measured sample for a repo from the in-memory cache.

        Falls back to reading storage.get_latest when the cache is cold (e.g.
        right after a daemon restart, before this repo has re-synced).

        Args:
            pkgid(str): Repository id.

        Return:
            sample(Optional[Sample]): Latest known sample, or None.
        """
        with self._cache_lock:
            sample = self._cache.get(pkgid)
        if sample is not None:
            return sample

        try:
            sample = storage.get_latest(self._config_loader().db_path(), pkgid)
        except Exception as exc:
            log.warning("Failed to read cached sample for %r from storage: %s", pkgid, exc)
            return None

        if sample is not None:
            with self._cache_lock:
                self._cache[pkgid] = sample
        return sample


# Module-level singleton wiring (used by the plug-in in __init__.py).

_worker: Optional[MeasureWorker] = None
_worker_lock = threading.Lock()


def get_worker() -> MeasureWorker:
    """Return the process-wide MeasureWorker, creating it on first use.

    Return:
        worker(MeasureWorker): The singleton worker.
    """
    global _worker
    if _worker is None:
        with _worker_lock:
            if _worker is None:
                _worker = MeasureWorker(config_loader=load_config)
    return _worker
