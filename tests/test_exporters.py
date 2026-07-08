"""Unit tests for mirror_plugin_statistics.exporters."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from mirror_plugin_statistics import exporters
from mirror_plugin_statistics.exporters import run_exporters
from mirror_plugin_statistics.exporters.json_export import export_json
from mirror_plugin_statistics.exporters.prometheus import (
    MEASURED_METRIC,
    SIZE_METRIC,
    export_prometheus,
)
from mirror_plugin_statistics.exporters.trend_image import export_trend_svg
from mirror_plugin_statistics.storage import Sample, insert_sample
from mirror_plugin_statistics.util import format_bytes


def _make_sample(pkgid: str, ts: float, bytes_: int, source: str = "du") -> Sample:
    """Build a Sample for tests."""
    return Sample(pkgid=pkgid, ts=ts, bytes=bytes_, file_count=None, source=source)


def _seed_db(db_path: Path) -> float:
    """Seed a tmp DB with two repos and a short history each; return "now"."""
    now = time.time()
    insert_sample(db_path, _make_sample("pkg1", now - 20, 100))
    insert_sample(db_path, _make_sample("pkg1", now - 10, 150))
    insert_sample(db_path, _make_sample("pkg1", now, 200, source="zfs"))
    insert_sample(db_path, _make_sample("pkg2", now - 5, 1024, source="du"))
    return now


# ---------------------------------------------------------------------------
# export_json
# ---------------------------------------------------------------------------

def test_export_json_shape_and_history(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    _seed_db(db_path)
    out_path = tmp_path / "usage.json"

    export_json(db_path, {"path": str(out_path)})

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert "generated_at" in data
    assert set(data["packages"].keys()) == {"pkg1", "pkg2"}

    pkg1 = data["packages"]["pkg1"]
    assert pkg1["bytes"] == 200
    assert pkg1["human"] == format_bytes(200)
    assert pkg1["source"] == "zfs"
    assert [item["bytes"] for item in pkg1["history"]] == [100, 150, 200]

    pkg2 = data["packages"]["pkg2"]
    assert pkg2["bytes"] == 1024
    assert pkg2["human"] == "1.0 KiB"


def test_export_json_caps_history_points(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    now = time.time()
    for i in range(5):
        insert_sample(db_path, _make_sample("pkg1", now + i, i))
    out_path = tmp_path / "usage.json"

    export_json(db_path, {"path": str(out_path), "history_points": 2})

    data = json.loads(out_path.read_text(encoding="utf-8"))
    history = data["packages"]["pkg1"]["history"]
    assert [item["bytes"] for item in history] == [3, 4]


# ---------------------------------------------------------------------------
# export_trend_svg
# ---------------------------------------------------------------------------

def test_export_trend_svg_with_data(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    _seed_db(db_path)
    out_path = tmp_path / "usage-trend.svg"

    export_trend_svg(db_path, {"path": str(out_path)})

    content = out_path.read_text(encoding="utf-8")
    assert content
    assert content.startswith("<svg") or content.startswith("<?xml")
    assert "<polyline" in content
    assert "pkg1" in content
    assert "pkg2" in content


def test_export_trend_svg_no_data_still_valid(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    out_path = tmp_path / "usage-trend.svg"

    export_trend_svg(db_path, {"path": str(out_path)})

    content = out_path.read_text(encoding="utf-8")
    assert content
    assert content.startswith("<svg") or content.startswith("<?xml")
    assert "<polyline" not in content
    assert "No data" in content


def test_export_trend_svg_respects_period_days_window(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    now = time.time()
    insert_sample(db_path, _make_sample("old_pkg", now - 100 * 86400, 100))
    out_path = tmp_path / "usage-trend.svg"

    export_trend_svg(db_path, {"path": str(out_path), "period_days": 1})

    content = out_path.read_text(encoding="utf-8")
    assert "<polyline" not in content
    assert "No data" in content


def test_export_trend_svg_escapes_pkgid(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    insert_sample(db_path, _make_sample("pkg<1>&2", time.time(), 100))
    out_path = tmp_path / "usage-trend.svg"

    export_trend_svg(db_path, {"path": str(out_path)})

    content = out_path.read_text(encoding="utf-8")
    assert "pkg<1>&2" not in content
    assert "pkg&lt;1&gt;&amp;2" in content


# ---------------------------------------------------------------------------
# export_prometheus
# ---------------------------------------------------------------------------

def test_export_prometheus_shape(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    _seed_db(db_path)
    out_path = tmp_path / "usage.prom"

    export_prometheus(db_path, {"path": str(out_path)})

    content = out_path.read_text(encoding="utf-8")
    assert f"# TYPE {SIZE_METRIC} gauge" in content
    assert f"# TYPE {MEASURED_METRIC} gauge" in content
    assert f'{SIZE_METRIC}{{repo="pkg1",source="zfs"}} 200' in content
    assert f'{SIZE_METRIC}{{repo="pkg2",source="du"}} 1024' in content
    assert f'{MEASURED_METRIC}{{repo="pkg1"}}' in content


def test_export_prometheus_escapes_label_values(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    insert_sample(db_path, _make_sample('pkg"1\\2', time.time(), 100, source='src"x'))
    out_path = tmp_path / "usage.prom"

    export_prometheus(db_path, {"path": str(out_path)})

    content = out_path.read_text(encoding="utf-8")
    assert 'repo="pkg\\"1\\\\2"' in content
    assert 'source="src\\"x"' in content


# ---------------------------------------------------------------------------
# run_exporters
# ---------------------------------------------------------------------------

def test_run_exporters_skips_unknown_and_isolates_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "usage.sqlite3"
    insert_sample(db_path, _make_sample("pkg1", time.time(), 100))

    calls: list[str] = []

    def _failing(db_path: Path, cfg: dict) -> None:
        calls.append("failing")
        raise RuntimeError("boom")

    def _ok(db_path: Path, cfg: dict) -> None:
        calls.append("ok")

    monkeypatch.setitem(exporters.EXPORTERS, "failing", _failing)
    monkeypatch.setitem(exporters.EXPORTERS, "ok", _ok)

    with caplog.at_level(logging.WARNING, logger="mirror"):
        run_exporters(
            db_path,
            {
                "unknown": {"path": str(tmp_path / "unknown.out")},
                "failing": {"path": str(tmp_path / "failing.out")},
                "ok": {"path": str(tmp_path / "ok.out")},
            },
        )

    assert calls == ["failing", "ok"]
    assert any("unknown" in message for message in caplog.messages)
    assert any("failing" in message for message in caplog.messages)


def test_run_exporters_runs_real_exporters_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "usage.sqlite3"
    _seed_db(db_path)

    run_exporters(
        db_path,
        {
            "json": {"path": str(tmp_path / "usage.json")},
            "trend_image": {"path": str(tmp_path / "usage-trend.svg")},
            "prometheus": {"path": str(tmp_path / "usage.prom")},
        },
    )

    assert (tmp_path / "usage.json").exists()
    assert (tmp_path / "usage-trend.svg").exists()
    assert (tmp_path / "usage.prom").exists()
