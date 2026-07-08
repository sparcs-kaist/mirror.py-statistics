"""SQLite time-series storage for per-repository disk-usage samples.

One row per (repo, measurement). The DB lives at ``<data_dir>/usage.sqlite3``
(see config.StatisticsConfig.db_path). WAL mode is enabled so a reader (the web
status hook / CLI) never blocks the single writer thread. Connections are opened
fresh per call because sqlite3 Connection objects are not shareable across
threads.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Seconds a blocked operation waits for a lock before raising.
BUSY_TIMEOUT_MS: int = 5000


@dataclass
class Sample:
    """A single disk-usage measurement for one repository.

    Args:
        pkgid(str): Package/repository id.
        ts(float): Unix timestamp (seconds) when measured.
        bytes(int): Measured size in bytes.
        file_count(Optional[int]): File count if the provider reported it, else None.
        source(str): Which provider produced this ("zfs"/"btrfs"/"xfs-quota"/"du"/"du-python").
    """

    pkgid: str
    ts: float
    bytes: int
    file_count: Optional[int]
    source: str


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + busy_timeout and row factory set.

    Ensures the parent directory exists. Internal helper.

    Args:
        db_path(Path): Database file path.

    Return:
        conn(sqlite3.Connection): Configured connection (caller closes it).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    """Create the schema if absent (idempotent).

    Table ``sample(pkgid TEXT, ts REAL, bytes INTEGER, file_count INTEGER NULL,
    source TEXT)`` with an index on ``(pkgid, ts)``.

    Args:
        db_path(Path): Database file path.
    """
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sample (
                pkgid TEXT NOT NULL,
                ts REAL NOT NULL,
                bytes INTEGER NOT NULL,
                file_count INTEGER,
                source TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sample_pkgid_ts ON sample(pkgid, ts)"
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_sample(row: sqlite3.Row) -> Sample:
    """Convert a sqlite3.Row from the sample table into a Sample."""
    return Sample(
        pkgid=row["pkgid"],
        ts=row["ts"],
        bytes=row["bytes"],
        file_count=row["file_count"],
        source=row["source"],
    )


def insert_sample(db_path: Path, sample: Sample) -> None:
    """Insert one sample row.

    Args:
        db_path(Path): Database file path.
        sample(Sample): Row to insert.
    """
    init_db(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sample (pkgid, ts, bytes, file_count, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (sample.pkgid, sample.ts, sample.bytes, sample.file_count, sample.source),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest(db_path: Path, pkgid: str) -> Optional[Sample]:
    """Return the most recent sample for a repo, or None if none recorded.

    Args:
        db_path(Path): Database file path.
        pkgid(str): Repository id.

    Return:
        sample(Optional[Sample]): Latest sample or None.
    """
    init_db(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT pkgid, ts, bytes, file_count, source FROM sample "
            "WHERE pkgid = ? ORDER BY ts DESC LIMIT 1",
            (pkgid,),
        ).fetchone()
        return _row_to_sample(row) if row is not None else None
    finally:
        conn.close()


def get_all_latest(db_path: Path) -> dict[str, Sample]:
    """Return the most recent sample for every repo.

    Args:
        db_path(Path): Database file path.

    Return:
        latest(dict[str, Sample]): Mapping pkgid -> latest Sample.
    """
    init_db(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.pkgid, s.ts, s.bytes, s.file_count, s.source
            FROM sample s
            INNER JOIN (
                SELECT pkgid, MAX(ts) AS max_ts FROM sample GROUP BY pkgid
            ) latest ON s.pkgid = latest.pkgid AND s.ts = latest.max_ts
            """
        ).fetchall()
        return {row["pkgid"]: _row_to_sample(row) for row in rows}
    finally:
        conn.close()


def get_history(
    db_path: Path,
    pkgid: str,
    since_ts: Optional[float] = None,
    limit: Optional[int] = None,
) -> list[Sample]:
    """Return samples for a repo in ascending time order.

    Args:
        db_path(Path): Database file path.
        pkgid(str): Repository id.
        since_ts(Optional[float]): If set, only samples with ts >= since_ts.
        limit(Optional[int]): If set, cap to the most recent N samples (still
            returned in ascending time order).

    Return:
        samples(list[Sample]): Matching samples, oldest first.
    """
    init_db(db_path)
    conn = _connect(db_path)
    try:
        query = "SELECT pkgid, ts, bytes, file_count, source FROM sample WHERE pkgid = ?"
        params: tuple = (pkgid,)
        if since_ts is not None:
            query += " AND ts >= ?"
            params += (since_ts,)

        if limit is not None:
            query += " ORDER BY ts DESC LIMIT ?"
            params += (limit,)
            rows = conn.execute(query, params).fetchall()
            samples = [_row_to_sample(row) for row in reversed(rows)]
            return samples

        query += " ORDER BY ts ASC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_sample(row) for row in rows]
    finally:
        conn.close()


def prune(db_path: Path, retention_days: int) -> int:
    """Delete samples older than retention_days.

    A no-op returning 0 when retention_days <= 0 (retain everything).

    Args:
        db_path(Path): Database file path.
        retention_days(int): Retention window in days.

    Return:
        deleted(int): Number of rows deleted.
    """
    if retention_days <= 0:
        return 0
    init_db(db_path)
    cutoff = time.time() - retention_days * 86400
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM sample WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
