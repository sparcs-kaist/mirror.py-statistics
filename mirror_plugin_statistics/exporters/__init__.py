"""Exporter registry.

Exporters read the SQLite history and write a rendered artifact (JSON, SVG
image, Prometheus textfile, ...). They run in the background worker after each
sample is recorded, NOT via mirror.py's synchronous StatusOutput mechanism
(which is JSON-only and runs on the status-write path). Each exporter writes
atomically and a failure in one is logged but never blocks the others.

Adding a new exporter = write a function ``export_x(db_path, exporter_cfg)`` and
register it in EXPORTERS below (plus a default filename in config.py).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .json_export import export_json
from .prometheus import export_prometheus
from .trend_image import export_trend_svg

log = logging.getLogger("mirror")

# Exporter name -> callable(db_path: Path, exporter_cfg: dict) -> None.
# exporter_cfg always contains a resolved "path" and "enabled" plus any extras.
EXPORTERS: dict[str, Callable[[Path, dict], None]] = {
    "json": export_json,
    "trend_image": export_trend_svg,
    "prometheus": export_prometheus,
}


def run_exporters(db_path: Path, enabled_exporters: dict[str, dict]) -> list[str]:
    """Run every enabled, known exporter, isolating failures.

    Args:
        db_path(Path): SQLite database path.
        enabled_exporters(dict[str, dict]): Mapping of exporter name -> resolved
            settings dict (each with a "path"). Names not in EXPORTERS are
            logged and skipped.

    Return:
        failed(list[str]): Names of unknown exporters and exporters that raised.
    """
    failed: list[str] = []
    for name, exporter_cfg in enabled_exporters.items():
        exporter = EXPORTERS.get(name)
        if exporter is None:
            log.warning("Unknown statistics exporter %r; skipping", name)
            failed.append(name)
            continue
        try:
            exporter(db_path, exporter_cfg)
        except Exception as exc:
            log.warning("Statistics exporter %r failed: %s", name, exc)
            failed.append(name)
    return failed
