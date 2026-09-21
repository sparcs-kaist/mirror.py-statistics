# mirror-plugin-statistics

A mirror.py **status** plug-in that measures how much disk space each mirrored
repository consumes, keeps a time-series history, and exposes it through several
output formats.

For the plug-in author guide, see
[`docs/PLUGINS.md`](https://github.com/sparcs-kaist/mirror.py/blob/main/docs/PLUGINS.md)
in the mirror.py repository.

## What it does

- **Measures on sync success.** On every `MASTER.PACKAGE_STATUS_UPDATE.POST`
  where a repo transitions to `ACTIVE`, a background worker measures the repo's
  on-disk directory (`package.settings.dst`).
- **Filesystem-native first, `du` fallback.** When a repo maps 1:1 to a ZFS
  dataset, a Btrfs subvolume, or an XFS project quota, the native "used" figure
  is read directly; otherwise it falls back to a `du`-style walk.
- **Stores history in SQLite** at `<data_dir>/usage.sqlite3` (default
  `/var/lib/mirror/statistics/`) — stdlib only, no RRDtool/external TSDB.
- **Exposes the data** through pluggable exporters: the live web `status.json`
  (nested under `plugins.statistics`), a dedicated `usage.json` with recent
  history, an SVG usage-trend chart, and an optional Prometheus textfile.

## Why SQLite (not RRDtool)

RRDtool needs a C dependency, one file per data source, and a fixed polling
step — a poor fit for mirror.py's dynamic repo set and irregular per-repo sync
schedules. SQLite is in the Python standard library, handles a dynamic repo set
and irregular sampling naturally, and exports to JSON for the existing web UI.

## Install

Into the mirror daemon's environment:

```bash
uv pip install -e .
# or
pip install -e .
```

Verify the entry point registered:

```bash
python -c "
from importlib.metadata import entry_points
print([(ep.name, ep.value) for ep in entry_points(group='mirror.status')])
"
# Expected to include: ('statistics', 'mirror_plugin_statistics:plugin')
```

## Configure

Enable it in `config.json` under `settings.plugins`:

```json
{
  "settings": {
    "plugins": {
      "statistics": {"enabled": true}
    }
  }
}
```

Per-plugin settings live in `statistics.json` next to `config.json`. Generate a
default file with:

```bash
mirror plugin config create statistics
```

Example `statistics.json`:

```json
{
  "data_dir": "/var/lib/mirror/statistics",
  "retention_days": 365,
  "measure": { "min_interval_seconds": 0, "providers": ["zfs", "btrfs", "xfs-quota", "du"] },
  "exporters": {
    "json":        { "enabled": true,  "path": "/var/lib/mirror/statistics/usage.json", "history_points": 200 },
    "trend_image": { "enabled": true,  "path": "/var/lib/mirror/statistics/usage-trend.svg", "period_days": 30 },
    "prometheus":  { "enabled": false, "path": "/var/lib/mirror/statistics/usage.prom" }
  }
}
```

All fields are optional; omit the file to use the defaults above.

`trend_image.path` may contain the placeholder `{pkgid}` (or its alias `{id}`),
e.g. `"/var/www/geoul/pkgs/{pkgid}/du.svg"`, to write one SVG per repository
instead of a single combined chart. Repo ids that would escape the target
directory (empty, `.`, `..`, or containing `/` or `\`) are skipped with a
warning.

## CLI

The distribution ships a `mirror-statistics` command that reads the same DB:

```bash
mirror-statistics list                 # latest size per repo
mirror-statistics history <pkgid>      # recorded history for one repo
mirror-statistics export               # re-run all enabled exporters now
```

By default, the CLI reads `statistics.json` next to `/etc/mirror/config.json`.
Use `--config PATH` when the main config is elsewhere. `--data-dir PATH`
overrides the database directory and default exporter paths; exporter paths
explicitly set in `statistics.json` are preserved.

## Uninstall

```bash
uv pip uninstall mirror-plugin-statistics
```

The plug-in disappears at the next daemon restart.
