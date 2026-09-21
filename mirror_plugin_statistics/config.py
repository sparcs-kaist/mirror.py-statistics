"""Configuration loading for the statistics plug-in.

Reads ``<config_dir>/statistics.json`` (the directory containing mirror.py's
main config.json) via ``mirror.plugin.get_config(NAME)``, layered over the
defaults below. Every field is optional in the operator's file.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mirror")

# Plug-in name; also the per-plugin config filename base (statistics.json).
NAME = "statistics"

# Default measurement provider priority order. First applicable wins; "du" is
# the always-applicable fallback and should stay last.
DEFAULT_PROVIDERS: list[str] = ["zfs", "btrfs", "xfs-quota", "du"]

DEFAULT_RETENTION_DAYS: int = 365

# Default subdirectory (under mirror.STATE_PATH) for the plug-in's data.
DEFAULT_STATE_SUBDIR: str = "statistics"

DB_FILENAME: str = "usage.sqlite3"

# Default exporter set. Each entry names an exporter type (which must match the
# exporters registry) and carries an ``enabled`` flag plus exporter-specific
# extras. ``path`` defaults (when omitted) to ``<data_dir>/<default filename>``.
DEFAULT_EXPORTERS: list[dict] = [
    {"type": "json", "enabled": True, "history_points": 200},
    {"type": "trend_image", "enabled": True, "period_days": 30},
    {"type": "prometheus", "enabled": False},
]

# Default output filename per exporter, joined onto data_dir when the operator
# does not set an explicit "path".
EXPORTER_DEFAULT_FILENAME: dict[str, str] = {
    "json": "usage.json",
    "trend_image": "usage-trend.svg",
    "prometheus": "usage.prom",
}


@dataclass
class StatisticsConfig:
    """Resolved statistics plug-in configuration.

    Args:
        data_dir(Path): Base directory for the SQLite DB and exporter outputs.
        retention_days(int): History retention window; older samples are pruned.
        providers(list[str]): Measurement provider priority order.
        min_interval_seconds(int): Minimum seconds between re-measuring the same
            repo; 0 disables throttling.
        exporters(list[dict]): Ordered resolved exporter settings. Every entry
            contains "type", "enabled", and "path".
    """

    data_dir: Path
    retention_days: int = DEFAULT_RETENTION_DAYS
    providers: list[str] = field(default_factory=lambda: list(DEFAULT_PROVIDERS))
    min_interval_seconds: int = 0
    exporters: list[dict] = field(default_factory=list)

    def db_path(self) -> Path:
        """Return the SQLite database path (``<data_dir>/usage.sqlite3``)."""
        return self.data_dir / DB_FILENAME

    def enabled_exporters(self) -> list[dict]:
        """Return enabled exporters in configured order, including duplicates."""
        return [cfg for cfg in self.exporters if cfg.get("enabled")]


def default_data_dir() -> Path:
    """Return the default data directory: ``mirror.STATE_PATH/statistics``.

    Import of ``mirror`` is deferred so this module is importable without a
    configured daemon (e.g. during packaging or isolated unit tests).

    Return:
        path(Path): Default data directory.
    """
    import mirror

    return mirror.STATE_PATH / DEFAULT_STATE_SUBDIR


def _coerce_retention_days(value: object) -> int:
    """Coerce a raw ``retention_days`` value, falling back to the default."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_RETENTION_DAYS


def _coerce_provider_list(value: object) -> list[str]:
    """Coerce a raw ``measure.providers`` value, falling back to the default."""
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return list(value)
    return list(DEFAULT_PROVIDERS)


def _coerce_min_interval_seconds(value: object) -> int:
    """Coerce a raw ``measure.min_interval_seconds`` value, falling back to 0."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _resolve_data_dir(raw: dict) -> Path:
    """Resolve ``data_dir`` from the raw config, falling back to the default."""
    value = raw.get("data_dir")
    if isinstance(value, str) and value:
        return Path(value)
    return default_data_dir()


def _exporter_output_path(name: str, path_value: object, data_dir: Path) -> str:
    """Resolve an exporter's output path, defaulting under data_dir.

    Args:
        name(str): Exporter name.
        path_value(object): Raw "path" value from config (may be missing/invalid).
        data_dir(Path): Base data directory used for the default filename.

    Return:
        path(str): Resolved output path.
    """
    if isinstance(path_value, str) and path_value:
        return path_value
    default_filename = EXPORTER_DEFAULT_FILENAME.get(name, name)
    return str(data_dir / default_filename)


def _resolve_exporters(raw_exporters: object, data_dir: Path) -> list[dict]:
    """Validate and resolve an ordered exporter configuration list.

    Args:
        raw_exporters(object): Raw "exporters" value from config.
        data_dir(Path): Base data directory used for default exporter paths.

    Return:
        exporters(list[dict]): Ordered resolved exporter settings, each carrying
            "type" (str), "enabled" (bool), and "path" (str) keys.

    Raises:
        ValueError: The exporter collection or one of its entries is invalid.
    """
    if isinstance(raw_exporters, dict):
        raise ValueError(
            "exporters must be a list; migrate the legacy object entries to "
            'list items with a "type" field'
        )
    if not isinstance(raw_exporters, list):
        raise ValueError("exporters must be a list")

    type_defaults = {
        entry["type"]: {
            key: copy.deepcopy(value)
            for key, value in entry.items()
            if key not in {"type", "enabled"}
        }
        for entry in DEFAULT_EXPORTERS
    }
    resolved: list[dict] = []
    for index, raw_settings in enumerate(raw_exporters):
        if not isinstance(raw_settings, dict):
            raise ValueError(f"exporters[{index}] must be an object")

        exporter_type = raw_settings.get("type")
        if not isinstance(exporter_type, str) or not exporter_type.strip():
            raise ValueError(
                f'exporters[{index}].type must be a non-empty string'
            )
        if "enabled" in raw_settings and not isinstance(raw_settings["enabled"], bool):
            raise ValueError(f"exporters[{index}].enabled must be a boolean")

        settings = copy.deepcopy(type_defaults.get(exporter_type, {}))
        settings.update(copy.deepcopy(raw_settings))
        settings["enabled"] = raw_settings.get("enabled", True)
        settings["path"] = _exporter_output_path(
            exporter_type, settings.get("path"), data_dir
        )
        resolved.append(settings)

    return resolved


def resolve_config(raw: object, data_dir_override: Path | None = None) -> StatisticsConfig:
    """Resolve a raw statistics config mapping over the defaults.

    Args:
        raw(object): Decoded statistics.json content.
        data_dir_override(Path | None): Optional data directory override applied
            before default exporter paths are resolved.

    Return:
        config(StatisticsConfig): Fully resolved configuration.

    Raises:
        ValueError: The ``exporters`` collection or one of its entries is
            invalid.
    """
    if not isinstance(raw, dict):
        raw = {}

    data_dir = data_dir_override if data_dir_override is not None else _resolve_data_dir(raw)

    measure_raw = raw.get("measure")
    if not isinstance(measure_raw, dict):
        measure_raw = {}

    return StatisticsConfig(
        data_dir=data_dir,
        retention_days=_coerce_retention_days(raw.get("retention_days")),
        providers=_coerce_provider_list(measure_raw.get("providers")),
        min_interval_seconds=_coerce_min_interval_seconds(measure_raw.get("min_interval_seconds")),
        exporters=_resolve_exporters(raw.get("exporters", DEFAULT_EXPORTERS), data_dir),
    )


def load_config() -> StatisticsConfig:
    """Load and resolve the daemon's statistics config over the defaults.

    Reads ``mirror.plugin.get_config(NAME)`` and layers it over DEFAULT_*.
    Resolves ``data_dir`` (default default_data_dir()), each exporter's
    ``enabled`` and ``path`` (default ``data_dir/<EXPORTER_DEFAULT_FILENAME>``),
    and the provider order. Invalid exporter configuration disables exporting
    while preserving the other resolved settings. Errors loading the operator
    file fall back to defaults.

    Return:
        config(StatisticsConfig): Fully resolved configuration.
    """
    import mirror.plugin

    try:
        raw = mirror.plugin.get_config(NAME)
    except Exception as exc:
        log.warning("Failed to load statistics plug-in config, using defaults: %s", exc)
        raw = {}

    try:
        return resolve_config(raw)
    except ValueError as exc:
        log.warning("Invalid statistics exporter configuration; disabling exporters: %s", exc)
        fallback_raw = dict(raw) if isinstance(raw, dict) else {}
        fallback_raw["exporters"] = []
        return resolve_config(fallback_raw)


def default_config_dict() -> dict:
    """Return the default statistics.json content as a JSON-serializable dict.

    Used by create_config() to seed a new operator config file. Paths are
    rendered as strings under the default data directory.

    Return:
        data(dict): Default config document.
    """
    data_dir = default_data_dir()

    exporters = copy.deepcopy(DEFAULT_EXPORTERS)
    for settings in exporters:
        name = settings["type"]
        settings["path"] = _exporter_output_path(name, None, data_dir)

    return {
        "data_dir": str(data_dir),
        "retention_days": DEFAULT_RETENTION_DAYS,
        "measure": {
            "min_interval_seconds": 0,
            "providers": list(DEFAULT_PROVIDERS),
        },
        "exporters": exporters,
    }
