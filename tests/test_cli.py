"""Unit tests for the mirror-statistics CLI."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

import pytest
from click.testing import CliRunner

from mirror_plugin_statistics import cli, config, exporters


def _write_statistics_config(config_dir: Path, data: dict) -> Path:
    """Write statistics.json and return the sibling main config path."""
    config_dir.mkdir(parents=True)
    (config_dir / "statistics.json").write_text(json.dumps(data), encoding="utf-8")
    return config_dir / "config.json"


def test_cli_reads_statistics_next_to_custom_main_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    main_config = _write_statistics_config(tmp_path / "custom-config", {"data_dir": str(data_dir)})
    seen: list[Path] = []
    output: list[str] = []

    def _get_all_latest(db_path: Path) -> dict:
        seen.append(db_path)
        return {}

    monkeypatch.setattr(cli.storage, "get_all_latest", _get_all_latest)
    monkeypatch.setattr(cli, "_print_line", lambda text, style="": output.append(text))

    result = CliRunner().invoke(cli.main, ["--config", str(main_config), "list"])

    assert result.exit_code == 0
    assert seen == [data_dir / "usage.sqlite3"]
    assert output == ["No usage data recorded yet."]


def test_cli_data_dir_override_rebases_only_default_exporter_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_data_dir = tmp_path / "configured"
    override_data_dir = tmp_path / "override"
    explicit_path = tmp_path / "public" / "trend.svg"
    main_config = _write_statistics_config(
        tmp_path / "config",
        {
            "data_dir": str(configured_data_dir),
            "exporters": {
                "json": {"enabled": True},
                "trend_image": {"enabled": True, "path": str(explicit_path)},
                "prometheus": {"enabled": False},
            },
        },
    )
    calls: dict[str, tuple[Path, dict]] = {}
    output: list[str] = []

    def _capture(name: str) -> Callable[[Path, dict], None]:
        def _export(db_path: Path, exporter_config: dict) -> None:
            calls[name] = (db_path, exporter_config)

        return _export

    monkeypatch.setitem(exporters.EXPORTERS, "json", _capture("json"))
    monkeypatch.setitem(exporters.EXPORTERS, "trend_image", _capture("trend_image"))
    monkeypatch.setattr(cli, "_print_line", lambda text, style="": output.append(text))

    result = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(main_config),
            "--data-dir",
            str(override_data_dir),
            "export",
        ],
    )

    assert result.exit_code == 0
    assert calls["json"] == (
        override_data_dir / "usage.sqlite3",
        {
            "enabled": True,
            "history_points": 200,
            "path": str(override_data_dir / "usage.json"),
        },
    )
    assert calls["trend_image"][0] == override_data_dir / "usage.sqlite3"
    assert calls["trend_image"][1]["path"] == str(explicit_path)
    assert f"json: {override_data_dir / 'usage.json'}" in output
    assert f"trend_image: {explicit_path}" in output


def test_cli_export_reports_partial_failures_and_prints_only_successes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_config = _write_statistics_config(
        tmp_path / "config",
        {
            "data_dir": str(tmp_path / "data"),
            "exporters": {
                "json": {"enabled": True},
                "trend_image": {"enabled": False},
                "broken": {"enabled": True},
                "unknown": {"enabled": True},
            },
        },
    )
    output: list[str] = []

    monkeypatch.setitem(exporters.EXPORTERS, "json", lambda db_path, cfg: None)
    monkeypatch.setattr(cli, "_print_line", lambda text, style="": output.append(text))

    def _fail(db_path: Path, exporter_config: dict) -> None:
        raise RuntimeError("boom")

    monkeypatch.setitem(exporters.EXPORTERS, "broken", _fail)

    result = CliRunner().invoke(cli.main, ["--config", str(main_config), "export"])

    assert result.exit_code != 0
    assert output == [f"json: {tmp_path / 'data' / 'usage.json'}"]
    assert "Exporters failed: broken, unknown" in result.stderr


def test_cli_missing_statistics_config_uses_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    seen: list[Path] = []

    def _get_all_latest(db_path: Path) -> dict:
        seen.append(db_path)
        return {}

    monkeypatch.setattr(cli.storage, "get_all_latest", _get_all_latest)

    with caplog.at_level(logging.WARNING, logger="mirror"):
        result = CliRunner().invoke(cli.main, ["--config", str(config_dir / "config.json"), "list"])

    assert result.exit_code == 0
    assert seen == [config.default_data_dir() / "usage.sqlite3"]
    assert not caplog.messages


def test_cli_bad_statistics_config_uses_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    main_config = _write_statistics_config(tmp_path / "config", {})
    (main_config.parent / "statistics.json").write_text("{not-json", encoding="utf-8")
    seen: list[Path] = []

    def _get_all_latest(db_path: Path) -> dict:
        seen.append(db_path)
        return {}

    monkeypatch.setattr(cli.storage, "get_all_latest", _get_all_latest)

    with caplog.at_level(logging.WARNING, logger="mirror"):
        result = CliRunner().invoke(cli.main, ["--config", str(main_config), "list"])

    assert result.exit_code == 0
    assert seen == [config.default_data_dir() / "usage.sqlite3"]
    assert any(
        "Failed to load statistics config, using defaults" in message
        for message in caplog.messages
    )


def test_cli_non_mapping_statistics_config_uses_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_config = tmp_path / "config" / "config.json"
    main_config.parent.mkdir()
    (main_config.parent / "statistics.json").write_text("[]", encoding="utf-8")
    seen: list[Path] = []

    def _get_all_latest(db_path: Path) -> dict:
        seen.append(db_path)
        return {}

    monkeypatch.setattr(cli.storage, "get_all_latest", _get_all_latest)

    result = CliRunner().invoke(cli.main, ["--config", str(main_config), "list"])

    assert result.exit_code == 0
    assert seen == [config.default_data_dir() / "usage.sqlite3"]


def test_cli_unreadable_statistics_config_uses_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    statistics_path = tmp_path / "config" / "statistics.json"
    main_config = _write_statistics_config(statistics_path.parent, {})
    original_read_text = Path.read_text
    seen: list[Path] = []

    def _read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == statistics_path:
            raise PermissionError("unreadable")
        return original_read_text(path, *args, **kwargs)

    def _get_all_latest(db_path: Path) -> dict:
        seen.append(db_path)
        return {}

    monkeypatch.setattr(Path, "read_text", _read_text)
    monkeypatch.setattr(cli.storage, "get_all_latest", _get_all_latest)

    with caplog.at_level(logging.WARNING, logger="mirror"):
        result = CliRunner().invoke(cli.main, ["--config", str(main_config), "list"])

    assert result.exit_code == 0
    assert seen == [config.default_data_dir() / "usage.sqlite3"]
    assert any(
        "Failed to load statistics config, using defaults" in message
        for message in caplog.messages
    )
