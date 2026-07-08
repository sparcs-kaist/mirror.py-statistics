"""`mirror-statistics` console script.

An out-of-band CLI (declared as this distribution's own console_script, since
mirror.py's plug-in system does not let plug-ins register subcommands on the
`mirror` CLI). Reads the same SQLite DB the daemon writes and can re-run the
exporters on demand.

Output uses prompt_toolkit with a plain-text fallback for terminals that do not
support styling.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.shortcuts import print_formatted_text

from . import config, exporters, storage
from .util import format_bytes

log = logging.getLogger("mirror")


def _styling_supported() -> bool:
    """Detect whether stdout is a TTY capable of ANSI styling.

    Return:
        supported(bool): True when styled output can be used; False when a
            plain-text fallback is required.
    """
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _print_line(text: str, style: str = "") -> None:
    """Print one line of text via prompt_toolkit, styled when supported.

    Falls back to a plain formatted string when stdout is not a TTY / does
    not support styling.

    Args:
        text(str): Line to print.
        style(str): prompt_toolkit style attributes (e.g. "bold underline"),
            ignored in the plain-text fallback.
    """
    if style and _styling_supported():
        print_formatted_text(FormattedText([(style, text)]))
    else:
        print_formatted_text(text)


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a simple aligned table with a styled (or plain) header row.

    Args:
        headers(list[str]): Column headers.
        rows(list[list[str]]): Row values; each row must have len(headers) cells.
    """
    widths = [
        max(len(header), *(len(row[i]) for row in rows)) if rows else len(header)
        for i, header in enumerate(headers)
    ]

    def _format_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths))

    _print_line(_format_row(headers), style="bold underline")
    for row in rows:
        _print_line(_format_row(row))


def _format_ts(ts: float) -> str:
    """Format a Unix timestamp as an ISO-8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@click.group()
@click.option(
    "--data-dir",
    default=None,
    help="Override the statistics data directory (else taken from config).",
)
@click.pass_context
def main(ctx: click.Context, data_dir: str | None) -> None:
    """Inspect and render mirror.py per-repository disk-usage statistics."""
    try:
        cfg = config.load_config()
    except Exception as exc:
        log.warning("Failed to load statistics config, using defaults: %s", exc)
        cfg = config.StatisticsConfig(data_dir=config.default_data_dir())

    if data_dir is not None:
        cfg.data_dir = Path(data_dir)

    ctx.obj = {"config": cfg, "db_path": cfg.db_path()}


@main.command("list")
@click.pass_context
def list_usage(ctx: click.Context) -> None:
    """Print a table of the latest measured size per repository."""
    db_path: Path = ctx.obj["db_path"]
    latest = storage.get_all_latest(db_path)
    if not latest:
        _print_line("No usage data recorded yet.")
        return

    rows = [
        [pkgid, format_bytes(sample.bytes), sample.source, _format_ts(sample.ts)]
        for pkgid, sample in sorted(latest.items())
    ]
    _print_table(["REPO", "SIZE", "SOURCE", "MEASURED_AT"], rows)


@main.command("history")
@click.argument("pkgid")
@click.option("--limit", type=int, default=20, help="Max rows (most recent).")
@click.pass_context
def history(ctx: click.Context, pkgid: str, limit: int) -> None:
    """Print the recorded size history for one repository."""
    db_path: Path = ctx.obj["db_path"]
    samples = storage.get_history(db_path, pkgid, limit=limit)
    if not samples:
        _print_line(f"No history recorded for {pkgid!r}.")
        return

    rows = [[_format_ts(sample.ts), format_bytes(sample.bytes)] for sample in samples]
    _print_table(["TIMESTAMP", "SIZE"], rows)


@main.command("export")
@click.pass_context
def export(ctx: click.Context) -> None:
    """Run all enabled exporters now (write JSON / SVG / Prometheus outputs)."""
    cfg: config.StatisticsConfig = ctx.obj["config"]
    db_path: Path = ctx.obj["db_path"]
    enabled = cfg.enabled_exporters()

    if not enabled:
        _print_line("No exporters enabled.")
        return

    exporters.run_exporters(db_path, enabled)
    for name, exporter_cfg in sorted(enabled.items()):
        _print_line(f"{name}: {exporter_cfg['path']}")
