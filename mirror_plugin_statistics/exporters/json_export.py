"""JSON exporter: current per-repo usage plus recent history."""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..storage import get_all_latest, get_history
from ..util import atomic_write_text, format_bytes

# Default number of most-recent history points per repo when the exporter
# config omits "history_points".
DEFAULT_HISTORY_POINTS: int = 200


def export_json(db_path: Path, exporter_cfg: dict) -> None:
    """Write a usage JSON document atomically to exporter_cfg["path"].

    Shape:
        {
          "generated_at": <unix ts>,
          "packages": {
            "<pkgid>": {
              "bytes": <int>, "human": "<str>", "measured_at": <ts>,
              "source": "<provider>",
              "history": [{"ts": <ts>, "bytes": <int>}, ...]   # oldest first
            }, ...
          }
        }

    Args:
        db_path(Path): SQLite database path.
        exporter_cfg(dict): Resolved settings; uses "path" and optional
            "history_points" (default DEFAULT_HISTORY_POINTS).
    """
    history_points = exporter_cfg.get("history_points")
    if not isinstance(history_points, int) or isinstance(history_points, bool) or history_points <= 0:
        history_points = DEFAULT_HISTORY_POINTS

    latest = get_all_latest(db_path)
    packages: dict[str, dict] = {}
    for pkgid, sample in latest.items():
        history = get_history(db_path, pkgid, limit=history_points)
        packages[pkgid] = {
            "bytes": sample.bytes,
            "human": format_bytes(sample.bytes),
            "measured_at": sample.ts,
            "source": sample.source,
            "history": [{"ts": item.ts, "bytes": item.bytes} for item in history],
        }

    document = {"generated_at": time.time(), "packages": packages}
    atomic_write_text(Path(exporter_cfg["path"]), json.dumps(document, indent=2))
