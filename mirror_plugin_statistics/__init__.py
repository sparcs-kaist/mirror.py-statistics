"""mirror.py status plug-in: per-repository disk-usage tracking.

Wiring only — the heavy lifting lives in the submodules:
  - measurement:  measure/ (FS-native providers + du fallback)
  - storage:      storage.py (SQLite time series)
  - background:   worker.py (measure on sync success, off the event path)
  - exporters:    exporters/ (JSON, SVG trend image, Prometheus)
  - config:       config.py

The plug-in:
  * setup() registers a MASTER.PACKAGE_STATUS_UPDATE.POST listener (only) — the
    worker thread is lazy-started on the first ACTIVE event, so loading the
    plug-in in CLI/config contexts spins up nothing.
  * on a sync success (new_status == "ACTIVE") it enqueues a measurement.
  * extend_web_status_fields() surfaces the latest cached size into status.json
    under web_status[pkgid]["plugins"]["statistics"].
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from . import worker
from .config import default_config_dict
from .util import atomic_write_text, format_bytes

log = logging.getLogger("mirror")

NAME = "statistics"

# Status literal signalling a completed, successful sync (see mirror.structure
# Package.set_status). Only this transition triggers a measurement.
SUCCESS_STATUS = "ACTIVE"


def _on_status(package, new_status) -> None:
    """MASTER.PACKAGE_STATUS_UPDATE.POST listener: enqueue on sync success.

    Args:
        package: The mirror.structure.Package whose status changed.
        new_status: The new status string.
    """
    if new_status != SUCCESS_STATUS:
        return

    dst = getattr(getattr(package, "settings", None), "dst", None)
    if not dst:
        log.warning("Package %r has no settings.dst; skipping usage measurement", getattr(package, "pkgid", "?"))
        return

    worker.get_worker().enqueue(package.pkgid, dst)


def setup() -> None:
    """Register the POST event listener. Starts no thread (see worker.py)."""
    import mirror.event

    mirror.event.on("MASTER.PACKAGE_STATUS_UPDATE.POST", _on_status)


def extend_web_status_fields(package) -> Optional[dict]:
    """Return the latest usage fields for a package, or None if never measured.

    Read-only and fast (in-memory cache / SQLite read) — never measures here.

    Args:
        package: The mirror.structure.Package being serialized.

    Return:
        fields(Optional[dict]): {"bytes", "human", "measured_at", "source"} or None.
    """
    sample = worker.get_worker().get_cached(package.pkgid)
    if sample is None:
        return None
    return {
        "bytes": sample.bytes,
        "human": format_bytes(sample.bytes),
        "measured_at": sample.ts,
        "source": sample.source,
    }


def _create_config(force: bool):
    """create_config hook for `mirror plugin config create statistics`.

    Writes the default statistics.json next to the main config.json. Skips
    (created=False) when the file exists and force is False.

    Args:
        force(bool): Overwrite an existing file when True.

    Return:
        result(mirror.plugin.ConfigCreateResult): path + created flag.
    """
    import json

    import mirror.config
    from mirror.plugin import ConfigCreateResult

    path = Path(mirror.config.CONFIG_PATH).parent / "statistics.json"
    if path.exists() and not force:
        return ConfigCreateResult(path=str(path), created=False)

    atomic_write_text(path, json.dumps(default_config_dict(), indent=4))
    return ConfigCreateResult(path=str(path), created=True)


def plugin():
    """Entry-point factory: return the status PluginRecord.

    Return:
        record(mirror.plugin.PluginRecord): The registered status plug-in.
    """
    from mirror.plugin import status_plugin

    return status_plugin(
        name=NAME,
        extend_web_status_fields=extend_web_status_fields,
        setup=setup,
        create_config=_create_config,
        config_filename="statistics.json",
        api_version=(1, 0),
    )
