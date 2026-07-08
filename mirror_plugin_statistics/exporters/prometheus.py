"""Prometheus textfile exporter (node_exporter textfile collector format)."""

from __future__ import annotations

from pathlib import Path

from ..storage import get_all_latest
from ..util import atomic_write_text

# Metric names.
SIZE_METRIC: str = "mirror_repo_size_bytes"
MEASURED_METRIC: str = "mirror_repo_size_measured_timestamp_seconds"


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text exposition format.

    Args:
        value(str): Raw label value.

    Return:
        escaped(str): Value with backslash, double-quote, and newline escaped.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def export_prometheus(db_path: Path, exporter_cfg: dict) -> None:
    """Write per-repo gauges in Prometheus textfile format atomically.

    Emits, for the latest sample of each repo::

        # HELP mirror_repo_size_bytes Measured on-disk size of the mirror repo in bytes.
        # TYPE mirror_repo_size_bytes gauge
        mirror_repo_size_bytes{repo="<pkgid>",source="<provider>"} <bytes>
        # HELP mirror_repo_size_measured_timestamp_seconds Unix time of the measurement.
        # TYPE mirror_repo_size_measured_timestamp_seconds gauge
        mirror_repo_size_measured_timestamp_seconds{repo="<pkgid>"} <ts>

    Label values are escaped per the Prometheus text exposition format.

    Args:
        db_path(Path): SQLite database path.
        exporter_cfg(dict): Resolved settings; uses "path".
    """
    latest = get_all_latest(db_path)
    pkgids = sorted(latest.keys())

    lines = [
        f"# HELP {SIZE_METRIC} Measured on-disk size of the mirror repo in bytes.",
        f"# TYPE {SIZE_METRIC} gauge",
    ]
    for pkgid in pkgids:
        sample = latest[pkgid]
        repo = _escape_label_value(pkgid)
        source = _escape_label_value(sample.source)
        lines.append(f'{SIZE_METRIC}{{repo="{repo}",source="{source}"}} {sample.bytes}')

    lines.append(f"# HELP {MEASURED_METRIC} Unix time of the measurement.")
    lines.append(f"# TYPE {MEASURED_METRIC} gauge")
    for pkgid in pkgids:
        sample = latest[pkgid]
        repo = _escape_label_value(pkgid)
        lines.append(f'{MEASURED_METRIC}{{repo="{repo}"}} {sample.ts}')

    text = "\n".join(lines) + "\n"
    atomic_write_text(Path(exporter_cfg["path"]), text)
