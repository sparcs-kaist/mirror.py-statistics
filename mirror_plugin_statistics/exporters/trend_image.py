"""Trend-image exporter: a self-contained SVG line chart of usage over time.

Hand-rolled SVG (plain XML string) so the plug-in needs no plotting
dependency. One line per repository over the configured period, with axes and
a legend. The output is a valid standalone .svg viewable in any browser.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

from ..storage import Sample, get_all_latest, get_history
from ..util import atomic_write_text, format_bytes

log = logging.getLogger("mirror")

# Default look-back window (days) when the exporter config omits "period_days".
DEFAULT_PERIOD_DAYS: int = 30

# Default canvas size.
DEFAULT_WIDTH: int = 960
DEFAULT_HEIGHT: int = 480

# Margins (pixels) reserved around the plot area for axis labels/legend.
_MARGIN_LEFT: int = 70
_MARGIN_RIGHT: int = 20
_MARGIN_TOP: int = 20
_MARGIN_BOTTOM: int = 40

# Vertical spacing between stacked legend entries.
_LEGEND_LINE_HEIGHT: int = 16

# Number of horizontal gridlines/labels drawn on the y axis.
_Y_TICK_COUNT: int = 4

# Colors cycled through for each repo's line/legend entry.
_PALETTE: list[str] = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


def _coerce_positive_number(value: object, default: float) -> float:
    """Return value if it is a positive int/float, else default. Internal helper."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _axis_lines(plot_left: float, plot_right: float, plot_top: float, plot_bottom: float) -> str:
    """Build the SVG markup for the plot's y and x axis lines. Internal helper."""
    return (
        f'<line x1="{plot_left}" y1="{plot_top}" x2="{plot_left}" y2="{plot_bottom}" '
        f'stroke="#333333" stroke-width="1" />'
        f'<line x1="{plot_left}" y1="{plot_bottom}" x2="{plot_right}" y2="{plot_bottom}" '
        f'stroke="#333333" stroke-width="1" />'
    )


def _render_empty_svg(width: int, height: int) -> str:
    """Render a valid placeholder SVG for the no-data-in-window case. Internal helper."""
    plot_left, plot_right = _MARGIN_LEFT, width - _MARGIN_RIGHT
    plot_top, plot_bottom = _MARGIN_TOP, height - _MARGIN_BOTTOM
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="12">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />',
        _axis_lines(plot_left, plot_right, plot_top, plot_bottom),
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" fill="#666666">'
        f'No data available</text>',
        "</svg>",
    ]
    return "\n".join(parts)


def _render_svg(
    series: dict[str, list[Sample]], width: int, height: int, since_ts: float, until_ts: float
) -> str:
    """Render the trend SVG document for one or more non-empty repo histories.

    Internal helper.

    Args:
        series(dict[str, list[Sample]]): Repo id -> samples (ascending time,
            non-empty), already restricted to the [since_ts, until_ts] window.
        width(int): Canvas width in pixels.
        height(int): Canvas height in pixels.
        since_ts(float): Window start (x axis minimum).
        until_ts(float): Window end (x axis maximum).

    Return:
        svg(str): A complete, standalone SVG document.
    """
    plot_left, plot_right = _MARGIN_LEFT, width - _MARGIN_RIGHT
    plot_top, plot_bottom = _MARGIN_TOP, height - _MARGIN_BOTTOM

    max_bytes = max(sample.bytes for history in series.values() for sample in history)
    max_bytes = max(max_bytes, 1)
    time_span = max(until_ts - since_ts, 1.0)

    def scale_x(ts: float) -> float:
        ratio = min(max((ts - since_ts) / time_span, 0.0), 1.0)
        return plot_left + ratio * (plot_right - plot_left)

    def scale_y(value: float) -> float:
        ratio = min(max(value / max_bytes, 0.0), 1.0)
        return plot_bottom - ratio * (plot_bottom - plot_top)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="12">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />',
        _axis_lines(plot_left, plot_right, plot_top, plot_bottom),
    ]

    for tick in range(_Y_TICK_COUNT + 1):
        value = max_bytes * tick / _Y_TICK_COUNT
        y = scale_y(value)
        label = _xml_escape(format_bytes(int(value)))
        parts.append(
            f'<line x1="{plot_left}" y1="{y:.2f}" x2="{plot_right}" y2="{y:.2f}" '
            f'stroke="#eeeeee" stroke-width="1" />'
        )
        parts.append(
            f'<text x="{plot_left - 8}" y="{y + 4:.2f}" text-anchor="end" fill="#333333">'
            f'{label}</text>'
        )

    for index, pkgid in enumerate(sorted(series.keys())):
        color = _PALETTE[index % len(_PALETTE)]
        history = series[pkgid]
        points = " ".join(
            f"{scale_x(sample.ts):.2f},{scale_y(sample.bytes):.2f}" for sample in history
        )
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" />'
        )

        legend_y = plot_top + index * _LEGEND_LINE_HEIGHT
        legend_label = _xml_escape(pkgid)
        parts.append(
            f'<rect x="{plot_right - 12}" y="{legend_y}" width="10" height="10" fill="{color}" />'
        )
        parts.append(
            f'<text x="{plot_right - 18}" y="{legend_y + 9}" text-anchor="end" fill="#333333">'
            f'{legend_label}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def _sanitize_pkgid_segment(pkgid: str) -> Optional[str]:
    """Validate that a repo id is safe to use as a single path segment.

    Rejects the empty string, "." and "..", and any id containing a path
    separator ("/" or "\\") or a NUL byte, since any of these could let a
    per-package path template escape the intended output directory.

    Args:
        pkgid(str): Raw repository id.

    Return:
        segment(Optional[str]): pkgid unchanged if safe to use as a path
            segment, else None.
    """
    if not pkgid or pkgid in (".", ".."):
        return None
    if "/" in pkgid or "\\" in pkgid or "\x00" in pkgid:
        return None
    return pkgid


def _export_per_package(
    db_path: Path, raw_path: str, width: int, height: int, since_ts: float, until_ts: float
) -> None:
    """Render one trend SVG per repository, substituting the path template.

    Internal helper. Repo ids that fail `_sanitize_pkgid_segment` are skipped
    (with a warning logged) instead of being written, so an unsafe id cannot
    escape the intended output directory. Repos with no history in the window
    still get an empty-state SVG.

    Args:
        db_path(Path): SQLite database path.
        raw_path(str): Path template containing "{pkgid}" and/or "{id}".
        width(int): Canvas width in pixels.
        height(int): Canvas height in pixels.
        since_ts(float): Window start (x axis minimum).
        until_ts(float): Window end (x axis maximum).
    """
    for pkgid in sorted(get_all_latest(db_path).keys()):
        safe = _sanitize_pkgid_segment(pkgid)
        if safe is None:
            log.warning(
                "trend_image: skipping repo %r with unsafe id for per-package export", pkgid
            )
            continue

        target = raw_path.replace("{pkgid}", safe).replace("{id}", safe)
        history = get_history(db_path, pkgid, since_ts=since_ts)
        if history:
            svg = _render_svg({pkgid: history}, width, height, since_ts, until_ts)
        else:
            svg = _render_empty_svg(width, height)
        atomic_write_text(Path(target), svg)


def export_trend_svg(db_path: Path, exporter_cfg: dict) -> None:
    """Render a usage-trend SVG atomically to exporter_cfg["path"].

    Reads each repo's history within the last ``period_days`` and draws one
    polyline per repo (bytes over time), with x (time) and y (bytes) axes and a
    legend. Y-axis labels use human-readable byte units. When there is no data
    in the window, still writes a valid SVG containing an empty-state message.

    When "path" contains the placeholder "{pkgid}" or "{id}", one SVG is
    rendered per repository instead (per-package mode), with the placeholder
    substituted by a sanitized repo id; otherwise all repos are drawn together
    into a single file at "path" (combined mode, unchanged).

    Args:
        db_path(Path): SQLite database path.
        exporter_cfg(dict): Resolved settings; uses "path" and optional
            "period_days" (default DEFAULT_PERIOD_DAYS), "width", "height".
    """
    period_days = _coerce_positive_number(exporter_cfg.get("period_days"), DEFAULT_PERIOD_DAYS)
    width = int(_coerce_positive_number(exporter_cfg.get("width"), DEFAULT_WIDTH))
    height = int(_coerce_positive_number(exporter_cfg.get("height"), DEFAULT_HEIGHT))

    until_ts = time.time()
    since_ts = until_ts - period_days * 86400

    raw_path = exporter_cfg["path"]
    if "{pkgid}" in raw_path or "{id}" in raw_path:
        _export_per_package(db_path, raw_path, width, height, since_ts, until_ts)
        return

    series: dict[str, list[Sample]] = {}
    for pkgid in sorted(get_all_latest(db_path).keys()):
        history = get_history(db_path, pkgid, since_ts=since_ts)
        if history:
            series[pkgid] = history

    if series:
        svg = _render_svg(series, width, height, since_ts, until_ts)
    else:
        svg = _render_empty_svg(width, height)

    atomic_write_text(Path(raw_path), svg)
