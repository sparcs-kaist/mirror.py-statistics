"""Trend-image exporter: a self-contained SVG line chart of usage over time.

Hand-rolled SVG (plain XML string) so the plug-in needs no plotting
dependency. Styled to resemble a classic RRDtool graph: a beveled plot frame,
"nice" round gridlines on both axes, a filled area under each series' line,
and an RRDtool-style GPRINT legend (Cur/Min/Avg/Max) below the plot. The
output is a valid standalone .svg viewable in any browser.
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

from ..storage import Sample, get_all_latest, get_history
from ..util import atomic_write_text, format_bytes

log = logging.getLogger("mirror")

# Default look-back window (days) when the exporter config omits "period_days".
DEFAULT_PERIOD_DAYS: int = 30

# Default canvas width and plot-area height baseline (title + plot + x labels).
# The legend and footer, rendered below this, extend the final document height.
DEFAULT_WIDTH: int = 960
DEFAULT_HEIGHT: int = 480

# Margins (pixels) reserved around the plot area for axis labels/title.
_MARGIN_LEFT: int = 72
_MARGIN_RIGHT: int = 16
_MARGIN_TOP: int = 34
_MARGIN_BOTTOM: int = 54

# Vertical spacing between stacked legend entries, and space reserved above
# the first legend row / below the last one (for the footer watermark).
_LEGEND_LINE_HEIGHT: int = 18
_LEGEND_TOP_PADDING: int = 14
_FOOTER_HEIGHT: int = 20

# Roughly how many major gridlines/labels to aim for on each axis.
_Y_TICK_COUNT: int = 5
_X_TICK_COUNT: int = 5

# Colors cycled through for each repo's area/line/legend swatch, chosen to
# resemble RRDtool's classic default color cycle.
_PALETTE: list[str] = [
    "#00cf00", "#0000ff", "#ff0000", "#00cccc", "#ff00ff",
    "#ffa500", "#a52a2a", "#666666",
]


def _coerce_positive_number(value: object, default: float) -> float:
    """Return value if it is a positive int/float, else default. Internal helper."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _nice_step(rough_step: float) -> float:
    """Round a rough tick step up to a "nice" 1/2/2.5/5 x 10^n value.

    Internal helper.

    Args:
        rough_step(float): Unrounded step size (> 0).

    Return:
        step(float): The smallest value from {1, 2, 2.5, 5} x 10^n that is
            greater than or equal to rough_step.
    """
    if rough_step <= 0:
        return 1.0
    magnitude = 10.0 ** math.floor(math.log10(rough_step))
    residual = rough_step / magnitude
    for candidate in (1.0, 2.0, 2.5, 5.0, 10.0):
        if residual <= candidate:
            return candidate * magnitude
    return 10.0 * magnitude


def _nice_ticks(max_value: float, target_count: int = _Y_TICK_COUNT) -> list[float]:
    """Compute round y-axis tick values spanning 0..at least max_value.

    Internal helper. Picks a step from {1, 2, 2.5, 5} x 10^n so the resulting
    ticks look like round numbers (RRDtool-style) instead of arbitrary
    fractions of the data's maximum.

    Args:
        max_value(float): Largest value the ticks must cover (> 0).
        target_count(int): Roughly how many major ticks to aim for.

    Return:
        ticks(list[float]): Ascending tick values from 0 up to >= max_value.
    """
    if max_value <= 0:
        return [0.0, 1.0]
    step = _nice_step(max_value / max(target_count, 1))
    ticks = [0.0]
    while ticks[-1] < max_value:
        ticks.append(ticks[-1] + step)
    return ticks


def _series_stats(samples: list[Sample]) -> tuple[int, int, float, int]:
    """Compute current/min/avg/max byte values for one series' samples.

    Internal helper.

    Args:
        samples(list[Sample]): Non-empty ascending-time sample list.

    Return:
        stats(tuple[int, int, float, int]): (current, minimum, average,
            maximum) byte values, where current is the last sample's value.
    """
    values = [sample.bytes for sample in samples]
    return values[-1], min(values), sum(values) / len(values), max(values)


def _x_ticks(since_ts: float, until_ts: float, tick_count: int = _X_TICK_COUNT) -> list[float]:
    """Compute evenly spaced timestamps across [since_ts, until_ts]. Internal helper."""
    span = until_ts - since_ts
    if tick_count <= 1 or span <= 0:
        return [since_ts]
    step = span / (tick_count - 1)
    return [since_ts + i * step for i in range(tick_count)]


def _format_x_label(ts: float, since_ts: float, until_ts: float) -> str:
    """Format a timestamp for an x-axis tick, choosing a date or time format.

    Internal helper. Uses "%m/%d" when the [since_ts, until_ts] window spans
    more than 2 days, else "%H:%M".
    """
    span_days = (until_ts - since_ts) / 86400.0
    fmt = "%m/%d" if span_days > 2 else "%H:%M"
    return time.strftime(fmt, time.localtime(ts))


def _bevel_border(left: float, top: float, right: float, bottom: float) -> str:
    """Build a subtle inset 3D bevel border around the plot rectangle.

    Internal helper. Light edges on top/left, darker edges on bottom/right,
    giving the plot area a slightly recessed look.
    """
    return (
        f'<line x1="{left:.2f}" y1="{top:.2f}" x2="{right:.2f}" y2="{top:.2f}" '
        f'stroke="#d9d9d9" stroke-width="1" />'
        f'<line x1="{left:.2f}" y1="{top:.2f}" x2="{left:.2f}" y2="{bottom:.2f}" '
        f'stroke="#d9d9d9" stroke-width="1" />'
        f'<line x1="{left:.2f}" y1="{bottom:.2f}" x2="{right:.2f}" y2="{bottom:.2f}" '
        f'stroke="#999999" stroke-width="1" />'
        f'<line x1="{right:.2f}" y1="{top:.2f}" x2="{right:.2f}" y2="{bottom:.2f}" '
        f'stroke="#999999" stroke-width="1" />'
    )


def _render_title(width: float, title: str) -> str:
    """Build the centered title text near the top of the canvas. Internal helper."""
    return (
        f'<text x="{width / 2:.2f}" y="22" text-anchor="middle" '
        f'font-size="14" font-weight="bold" fill="#333333">{_xml_escape(title)}</text>'
    )


def _render_y_grid(
    ticks: list[float], plot_left: float, plot_right: float, plot_top: float, plot_bottom: float
) -> list[str]:
    """Build major/minor horizontal gridlines and value labels for the y axis.

    Internal helper. Major gridlines are solid and labeled with format_bytes;
    a faint minor gridline is drawn halfway between each pair of majors.
    """
    axis_max = ticks[-1]
    parts: list[str] = []

    def scale_y(value: float) -> float:
        ratio = min(max(value / axis_max, 0.0), 1.0) if axis_max > 0 else 0.0
        return plot_bottom - ratio * (plot_bottom - plot_top)

    for index, tick in enumerate(ticks):
        y = scale_y(tick)
        parts.append(
            f'<line x1="{plot_left:.2f}" y1="{y:.2f}" x2="{plot_right:.2f}" y2="{y:.2f}" '
            f'stroke="#c0c0c0" stroke-width="1" />'
        )
        label = _xml_escape(format_bytes(int(tick)))
        parts.append(
            f'<text x="{plot_left - 8:.2f}" y="{y + 4:.2f}" text-anchor="end" '
            f'font-size="11" fill="#333333">{label}</text>'
        )
        if index + 1 < len(ticks):
            mid_y = scale_y((tick + ticks[index + 1]) / 2)
            parts.append(
                f'<line x1="{plot_left:.2f}" y1="{mid_y:.2f}" x2="{plot_right:.2f}" y2="{mid_y:.2f}" '
                f'stroke="#ececec" stroke-width="1" />'
            )
    return parts


def _render_x_grid(
    since_ts: float,
    until_ts: float,
    plot_left: float,
    plot_right: float,
    plot_top: float,
    plot_bottom: float,
) -> list[str]:
    """Build vertical gridlines and date/time labels for the x (time) axis.

    Internal helper. The first/last tick labels are anchored to stay inside
    the plot's horizontal bounds instead of overflowing past the canvas edge.
    """
    ticks = _x_ticks(since_ts, until_ts)
    span = max(until_ts - since_ts, 1.0)
    parts: list[str] = []

    def scale_x(ts: float) -> float:
        ratio = min(max((ts - since_ts) / span, 0.0), 1.0)
        return plot_left + ratio * (plot_right - plot_left)

    for index, tick in enumerate(ticks):
        x = scale_x(tick)
        parts.append(
            f'<line x1="{x:.2f}" y1="{plot_top:.2f}" x2="{x:.2f}" y2="{plot_bottom:.2f}" '
            f'stroke="#c0c0c0" stroke-width="1" />'
        )
        if index == 0:
            anchor = "start"
        elif index == len(ticks) - 1:
            anchor = "end"
        else:
            anchor = "middle"
        label = _xml_escape(_format_x_label(tick, since_ts, until_ts))
        parts.append(
            f'<text x="{x:.2f}" y="{plot_bottom + 16:.2f}" text-anchor="{anchor}" '
            f'font-size="11" fill="#333333">{label}</text>'
        )
    return parts


def _render_series(
    series: dict[str, list[Sample]],
    plot_left: float,
    plot_right: float,
    plot_top: float,
    plot_bottom: float,
    since_ts: float,
    until_ts: float,
    axis_max: float,
) -> list[str]:
    """Build the area fill + line for each repo's series, palette-cycled.

    Internal helper. Each series is drawn as a semi-transparent area (a
    closed <polygon> from the line down to the plot baseline) with the line
    itself on top as a <polyline>, both in the same hue.
    """
    span = max(until_ts - since_ts, 1.0)
    parts: list[str] = []

    def scale_x(ts: float) -> float:
        ratio = min(max((ts - since_ts) / span, 0.0), 1.0)
        return plot_left + ratio * (plot_right - plot_left)

    def scale_y(value: float) -> float:
        ratio = min(max(value / axis_max, 0.0), 1.0) if axis_max > 0 else 0.0
        return plot_bottom - ratio * (plot_bottom - plot_top)

    for index, pkgid in enumerate(sorted(series.keys())):
        color = _PALETTE[index % len(_PALETTE)]
        points = [(scale_x(sample.ts), scale_y(sample.bytes)) for sample in series[pkgid]]

        area_vertices = [f"{points[0][0]:.2f},{plot_bottom:.2f}"]
        area_vertices.extend(f"{x:.2f},{y:.2f}" for x, y in points)
        area_vertices.append(f"{points[-1][0]:.2f},{plot_bottom:.2f}")
        parts.append(
            f'<polygon points="{" ".join(area_vertices)}" fill="{color}" '
            f'fill-opacity="0.15" stroke="none" />'
        )

        line_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        parts.append(
            f'<polyline points="{line_points}" fill="none" stroke="{color}" stroke-width="1.5" />'
        )
    return parts


def _format_legend_stats(cur: int, min_: int, avg: float, max_: int) -> str:
    """Format the Cur/Min/Avg/Max GPRINT-style stats row for one series.

    Internal helper. Values are right-justified to a fixed width so, when
    rendered in a monospace font, the numeric columns line up across rows.
    """
    cur_s, min_s, avg_s, max_s = (
        format_bytes(int(cur)), format_bytes(int(min_)), format_bytes(int(avg)), format_bytes(int(max_)),
    )
    return f"Cur: {cur_s:>9}   Min: {min_s:>9}   Avg: {avg_s:>9}   Max: {max_s:>9}"


def _render_legend(
    series: dict[str, list[Sample]], plot_left: float, plot_right: float, legend_top: float
) -> list[str]:
    """Build one legend row per series: color swatch + name + Cur/Min/Avg/Max.

    Internal helper. Mirrors RRDtool's GPRINT legend layout: the repo name is
    left-aligned next to a color swatch, the stats column is right-aligned in
    a monospace font so values line up across rows.
    """
    parts: list[str] = []
    for index, pkgid in enumerate(sorted(series.keys())):
        color = _PALETTE[index % len(_PALETTE)]
        row_y = legend_top + index * _LEGEND_LINE_HEIGHT
        cur, min_, avg, max_ = _series_stats(series[pkgid])

        parts.append(
            f'<rect x="{plot_left:.2f}" y="{row_y:.2f}" width="10" height="10" fill="{color}" />'
        )
        parts.append(
            f'<text x="{plot_left + 16:.2f}" y="{row_y + 9:.2f}" text-anchor="start" '
            f'font-size="11" fill="#333333">{_xml_escape(pkgid)}</text>'
        )
        stats_text = _xml_escape(_format_legend_stats(cur, min_, avg, max_))
        parts.append(
            f'<text x="{plot_right:.2f}" y="{row_y + 9:.2f}" text-anchor="end" '
            f'font-family="monospace" font-size="11" fill="#333333" xml:space="preserve">'
            f'{stats_text}</text>'
        )
    return parts


def _render_footer(width: float, total_height: float, now_ts: float) -> str:
    """Build the small bottom-right generation-time watermark. Internal helper."""
    timestamp = _xml_escape(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)))
    return (
        f'<text x="{width - 8:.2f}" y="{total_height - 6:.2f}" text-anchor="end" '
        f'font-size="9" fill="#999999">Generated {timestamp}</text>'
    )


def _render_empty_svg(width: int, height: int) -> str:
    """Render a valid placeholder SVG for the no-data-in-window case.

    Internal helper. Keeps the same frame/title/border as a populated graph,
    but shows a centered "No data available" message instead of a plot.
    """
    plot_left, plot_right = float(_MARGIN_LEFT), float(width - _MARGIN_RIGHT)
    plot_top, plot_bottom = float(_MARGIN_TOP), float(height - _MARGIN_BOTTOM)
    total_height = float(height)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{total_height:.2f}" '
        f'viewBox="0 0 {width} {total_height:.2f}" font-family="sans-serif" font-size="12">',
        f'<rect x="0" y="0" width="{width}" height="{total_height:.2f}" fill="#f5f5f5" />',
        f'<rect x="{plot_left:.2f}" y="{plot_top:.2f}" '
        f'width="{plot_right - plot_left:.2f}" height="{plot_bottom - plot_top:.2f}" fill="#ffffff" />',
        _render_title(width, "Disk usage"),
        f'<text x="{(plot_left + plot_right) / 2:.2f}" y="{(plot_top + plot_bottom) / 2:.2f}" '
        f'text-anchor="middle" fill="#666666">No data available</text>',
        _bevel_border(plot_left, plot_top, plot_right, plot_bottom),
        _render_footer(width, total_height, time.time()),
        "</svg>",
    ]
    return "\n".join(parts)


def _render_svg(
    series: dict[str, list[Sample]], width: int, height: int, since_ts: float, until_ts: float
) -> str:
    """Render the trend SVG document for one or more non-empty repo histories.

    Internal helper. Draws an RRDtool-style graph: beveled plot frame, nice
    round gridlines on both axes, a filled area + line per series, and a
    Cur/Min/Avg/Max legend below the plot. The legend (and footer watermark)
    extend the canvas past `height`, which is treated as the plot-area
    baseline (title + plot + x labels) rather than the final document height.

    Args:
        series(dict[str, list[Sample]]): Repo id -> samples (ascending time,
            non-empty), already restricted to the [since_ts, until_ts] window.
        width(int): Canvas width in pixels.
        height(int): Plot-area baseline height in pixels (before legend/footer).
        since_ts(float): Window start (x axis minimum).
        until_ts(float): Window end (x axis maximum).

    Return:
        svg(str): A complete, standalone SVG document.
    """
    plot_left, plot_right = float(_MARGIN_LEFT), float(width - _MARGIN_RIGHT)
    plot_top, plot_bottom = float(_MARGIN_TOP), float(height - _MARGIN_BOTTOM)

    pkgids = sorted(series.keys())
    title = pkgids[0] if len(pkgids) == 1 else "Disk usage"

    max_bytes = max(sample.bytes for history in series.values() for sample in history)
    ticks = _nice_ticks(max(max_bytes, 1))
    axis_max = ticks[-1]

    legend_top = height + _LEGEND_TOP_PADDING
    total_height = legend_top + len(pkgids) * _LEGEND_LINE_HEIGHT + _FOOTER_HEIGHT

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{total_height:.2f}" '
        f'viewBox="0 0 {width} {total_height:.2f}" font-family="sans-serif" font-size="12">',
        f'<rect x="0" y="0" width="{width}" height="{total_height:.2f}" fill="#f5f5f5" />',
        f'<rect x="{plot_left:.2f}" y="{plot_top:.2f}" '
        f'width="{plot_right - plot_left:.2f}" height="{plot_bottom - plot_top:.2f}" fill="#ffffff" />',
        _render_title(width, title),
    ]
    parts.extend(_render_y_grid(ticks, plot_left, plot_right, plot_top, plot_bottom))
    parts.extend(_render_x_grid(since_ts, until_ts, plot_left, plot_right, plot_top, plot_bottom))
    parts.extend(
        _render_series(series, plot_left, plot_right, plot_top, plot_bottom, since_ts, until_ts, axis_max)
    )
    parts.append(_bevel_border(plot_left, plot_top, plot_right, plot_bottom))
    parts.extend(_render_legend(series, plot_left, plot_right, legend_top))
    parts.append(_render_footer(width, total_height, time.time()))
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
    line per repo (bytes over time), styled as an RRDtool-like graph with
    gridlines, a filled area per series, and a Cur/Min/Avg/Max legend. Y-axis
    labels use human-readable byte units. When there is no data in the
    window, still writes a valid SVG containing an empty-state message.

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
