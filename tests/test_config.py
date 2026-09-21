"""Unit tests for mirror_plugin_statistics.config and .util."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

import mirror
import mirror.plugin

from mirror_plugin_statistics.config import (
    DEFAULT_PROVIDERS,
    DEFAULT_RETENTION_DAYS,
    default_config_dict,
    default_data_dir,
    load_config,
    resolve_config,
)
from mirror_plugin_statistics.util import atomic_write_bytes, atomic_write_text, format_bytes


# ---------------------------------------------------------------------------
# util.format_bytes
# ---------------------------------------------------------------------------

def test_format_bytes_sub_kib_has_no_decimal() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1023) == "1023 B"


def test_format_bytes_kib() -> None:
    assert format_bytes(1024) == "1.0 KiB"


def test_format_bytes_gib() -> None:
    assert format_bytes(int(1.5 * 1024**3)) == "1.5 GiB"


def test_format_bytes_pib() -> None:
    assert format_bytes(2 * 1024**5) == "2.0 PiB"


# ---------------------------------------------------------------------------
# util.atomic_write_bytes / atomic_write_text
# ---------------------------------------------------------------------------

def test_atomic_write_bytes_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "usage.bin"
    atomic_write_bytes(target, b"hello world")
    assert target.read_bytes() == b"hello world"


def test_atomic_write_bytes_sets_mode(tmp_path: Path) -> None:
    target = tmp_path / "usage.bin"
    atomic_write_bytes(target, b"data", mode=0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_atomic_write_bytes_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "usage.bin"
    atomic_write_bytes(target, b"first")
    atomic_write_bytes(target, b"second")
    assert target.read_bytes() == b"second"


def test_atomic_write_bytes_leaves_no_tempfile(tmp_path: Path) -> None:
    target = tmp_path / "usage.bin"
    atomic_write_bytes(target, b"data")
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_text_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "usage.txt"
    atomic_write_text(target, "hello", mode=0o644)
    assert target.read_text(encoding="utf-8") == "hello"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


# ---------------------------------------------------------------------------
# config.default_data_dir
# ---------------------------------------------------------------------------

def test_default_data_dir_uses_state_path() -> None:
    assert default_data_dir() == mirror.STATE_PATH / "statistics"


# ---------------------------------------------------------------------------
# config.load_config
# ---------------------------------------------------------------------------

def test_load_config_defaults_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirror.plugin, "get_config", lambda name: {})

    cfg = load_config()

    assert cfg.data_dir == default_data_dir()
    assert cfg.retention_days == DEFAULT_RETENTION_DAYS
    assert cfg.providers == DEFAULT_PROVIDERS
    assert cfg.min_interval_seconds == 0
    assert cfg.exporters["json"]["enabled"] is True
    assert cfg.exporters["json"]["path"] == str(default_data_dir() / "usage.json")
    assert cfg.exporters["trend_image"]["path"] == str(default_data_dir() / "usage-trend.svg")
    assert cfg.exporters["prometheus"]["enabled"] is False
    assert cfg.exporters["prometheus"]["path"] == str(default_data_dir() / "usage.prom")


def test_load_config_applies_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = {
        "data_dir": str(tmp_path),
        "retention_days": 30,
        "measure": {
            "min_interval_seconds": 60,
            "providers": ["du"],
        },
        "exporters": {
            "json": {"enabled": False},
            "prometheus": {"enabled": True, "path": "/custom/usage.prom"},
            "custom_exporter": {"enabled": True},
        },
    }
    monkeypatch.setattr(mirror.plugin, "get_config", lambda name: raw)

    cfg = load_config()

    assert cfg.data_dir == tmp_path
    assert cfg.retention_days == 30
    assert cfg.providers == ["du"]
    assert cfg.min_interval_seconds == 60
    assert cfg.exporters["json"]["enabled"] is False
    assert cfg.exporters["json"]["path"] == str(tmp_path / "usage.json")
    assert cfg.exporters["prometheus"]["enabled"] is True
    assert cfg.exporters["prometheus"]["path"] == "/custom/usage.prom"
    assert cfg.exporters["custom_exporter"]["enabled"] is True
    assert cfg.exporters["custom_exporter"]["path"] == str(tmp_path / "custom_exporter")


def test_load_config_tolerates_bad_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirror.plugin, "get_config", lambda name: {"retention_days": "not-a-number", "measure": "bad"})

    cfg = load_config()

    assert cfg.retention_days == DEFAULT_RETENTION_DAYS
    assert cfg.providers == DEFAULT_PROVIDERS
    assert cfg.min_interval_seconds == 0


def test_load_config_never_raises_on_get_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(name: str) -> dict:
        raise KeyError(name)

    monkeypatch.setattr(mirror.plugin, "get_config", _raise)

    cfg = load_config()

    assert cfg.data_dir == default_data_dir()
    assert cfg.retention_days == DEFAULT_RETENTION_DAYS


def test_resolve_config_applies_data_dir_override_before_exporter_paths(tmp_path: Path) -> None:
    override = tmp_path / "override"
    explicit = tmp_path / "explicit.svg"

    cfg = resolve_config(
        {
            "data_dir": str(tmp_path / "configured"),
            "exporters": {
                "trend_image": {"path": str(explicit)},
            },
        },
        data_dir_override=override,
    )

    assert cfg.data_dir == override
    assert cfg.exporters["json"]["path"] == str(override / "usage.json")
    assert cfg.exporters["trend_image"]["path"] == str(explicit)


# ---------------------------------------------------------------------------
# config.default_config_dict
# ---------------------------------------------------------------------------

def test_default_config_dict_shape() -> None:
    data = default_config_dict()

    data_dir = default_data_dir()
    assert data["data_dir"] == str(data_dir)
    assert data["retention_days"] == DEFAULT_RETENTION_DAYS
    assert data["measure"] == {
        "min_interval_seconds": 0,
        "providers": list(DEFAULT_PROVIDERS),
    }
    assert data["exporters"]["json"]["path"] == str(data_dir / "usage.json")
    assert data["exporters"]["trend_image"]["path"] == str(data_dir / "usage-trend.svg")
    assert data["exporters"]["prometheus"]["path"] == str(data_dir / "usage.prom")
    assert data["exporters"]["json"]["enabled"] is True
    assert data["exporters"]["prometheus"]["enabled"] is False
