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


def run_exporters(db_path: Path, enabled_exporters: list[dict]) -> list[int]:
    """Run every enabled, known exporter, isolating failures.

    Args:
        db_path(Path): SQLite database path.
        enabled_exporters(list[dict]): Ordered resolved exporter settings. Each
            entry contains a "type" and "path". Types not in EXPORTERS are
            logged and skipped.

    Return:
        failed(list[int]): Indexes of unknown exporters and exporters that
            raised.
    """
    failed: list[int] = []
    for index, exporter_cfg in enumerate(enabled_exporters):
        exporter_type = exporter_cfg["type"]
        output_path = exporter_cfg["path"]
        exporter = EXPORTERS.get(exporter_type)
        if exporter is None:
            log.warning(
                "Unknown statistics exporter type %r for path %r; skipping",
                exporter_type,
                output_path,
            )
            failed.append(index)
            continue
        try:
            exporter(db_path, exporter_cfg)
        except Exception as exc:
            log.warning(
                "Statistics exporter type %r for path %r failed: %s",
                exporter_type,
                output_path,
                exc,
            )
            failed.append(index)
    return failed
