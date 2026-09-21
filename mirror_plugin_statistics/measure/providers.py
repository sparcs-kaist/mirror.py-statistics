"""Disk-usage measurement providers.

Each provider knows how to measure one kind of storage. ``measure_usage`` (see
measure/__init__.py) tries them in the configured order; the first whose
``applicable()`` is true and whose ``measure()`` returns a result wins,
otherwise the ``du`` fallback (always applicable) is used.

Security contract for every provider that shells out:
  - subprocess.run with an ARGUMENT LIST, never shell=True.
  - always pass a timeout=; on timeout / non-zero exit / unparseable output,
    return None (caller falls through to the next provider). Never raise.
  - identifiers handed to zfs/btrfs/xfs_quota (dataset name, project id, ...)
    are derived ONLY from trusted system metadata (/proc/mounts, `zfs list`
    output, the XFS project tables) matched against the resolved real path of
    the destination — never interpolated from the raw config/dst string.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("mirror")

# Max seconds any provider subprocess may run before it is killed and the
# provider degrades to the next one.
SUBPROCESS_TIMEOUT: float = 300.0

# Overridable in tests: path to the mounts table and the XFS project table.
_PROC_MOUNTS_PATH: str = "/proc/mounts"
_XFS_PROJECTS_PATH: str = "/etc/projects"

# Matches /proc/mounts octal escape sequences, e.g. "\040" for a space.
_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


@dataclass
class MeasureResult:
    """Outcome of a successful measurement.

    Args:
        bytes(int): Measured size in bytes.
        file_count(Optional[int]): File count when known (du-python), else None.
        source(str): Provider identifier ("zfs"/"btrfs"/"xfs-quota"/"du"/"du-python").
    """

    bytes: int
    file_count: Optional[int]
    source: str


@dataclass
class MountInfo:
    """One parsed /proc/mounts entry (subset).

    Args:
        mountpoint(str): Absolute mountpoint path.
        fstype(str): Filesystem type (e.g. "zfs", "btrfs", "xfs").
        source(str): Mount source/device field (e.g. a zfs dataset name).
    """

    mountpoint: str
    fstype: str
    source: str


def _unescape_octal(field: str) -> str:
    """Unescape /proc/mounts octal escape sequences (e.g. "\\040" -> " ")."""
    return _OCTAL_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), field)


def read_mounts() -> list[MountInfo]:
    """Parse /proc/mounts into a list of MountInfo. Returns [] on any read error."""
    try:
        with open(_PROC_MOUNTS_PATH, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError as exc:
        log.warning("Failed to read %s: %s", _PROC_MOUNTS_PATH, exc)
        return []

    mounts: list[MountInfo] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        source, mountpoint, fstype = fields[0], fields[1], fields[2]
        mounts.append(
            MountInfo(
                mountpoint=_unescape_octal(mountpoint),
                fstype=fstype,
                source=_unescape_octal(source),
            )
        )
    return mounts


def _is_path_under_mount(mountpoint: str, real_path: str) -> bool:
    """Return True if mountpoint is a path-boundary-safe prefix of real_path."""
    if mountpoint == "/":
        return True
    return real_path == mountpoint or real_path.startswith(mountpoint + "/")


def find_mount_for_path(real_path: str, mounts: list[MountInfo]) -> Optional[MountInfo]:
    """Return the MountInfo whose mountpoint is the longest prefix of real_path.

    Args:
        real_path(str): Resolved absolute path.
        mounts(list[MountInfo]): Parsed mounts.

    Return:
        mount(Optional[MountInfo]): Best-matching mount, or None.
    """
    best: Optional[MountInfo] = None
    for mount in mounts:
        if not _is_path_under_mount(mount.mountpoint, real_path):
            continue
        if best is None or len(mount.mountpoint) > len(best.mountpoint):
            best = mount
    return best


def _get_mount_for_dst(real_dst: str) -> Optional[MountInfo]:
    """Look up the mount (longest matching prefix) that covers real_dst."""
    return find_mount_for_path(real_dst, read_mounts())


class Provider:
    """Base measurement provider interface.

    Subclasses set ``name`` and implement ``applicable`` and ``measure``.
    ``measure`` receives an already-resolved, validated real directory path.
    """

    name: str = ""

    def applicable(self, real_dst: str) -> bool:
        """Return True if this provider can accurately measure real_dst."""
        raise NotImplementedError

    def measure(self, real_dst: str) -> Optional[MeasureResult]:
        """Measure real_dst, or return None on any failure (caller falls through)."""
        raise NotImplementedError


def _measure_with_zfs(dataset: str) -> Optional[MeasureResult]:
    """Run `zfs get used` for a trusted dataset name and return its size."""
    zfs_binary = shutil.which("zfs")
    if zfs_binary is None:
        log.warning("zfs binary not found; cannot measure dataset %s", dataset)
        return None
    try:
        completed = subprocess.run(
            [zfs_binary, "get", "-Hp", "-o", "value", "used", dataset],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("zfs get failed for dataset %s: %s", dataset, exc)
        return None
    if completed.returncode != 0:
        log.warning(
            "zfs get exited %d for dataset %s: %s",
            completed.returncode, dataset, completed.stderr.strip(),
        )
        return None
    try:
        used_bytes = int(completed.stdout.strip())
    except ValueError as exc:
        log.warning("Failed to parse zfs used value for dataset %s: %s", dataset, exc)
        return None
    return MeasureResult(bytes=used_bytes, file_count=None, source="zfs")


class ZfsProvider(Provider):
    """Measures a repo that is exactly a ZFS dataset mountpoint (used property)."""

    name = "zfs"

    def applicable(self, real_dst: str) -> bool:
        mount = _get_mount_for_dst(real_dst)
        return mount is not None and mount.fstype == "zfs" and mount.mountpoint == real_dst

    def measure(self, real_dst: str) -> Optional[MeasureResult]:
        mount = _get_mount_for_dst(real_dst)
        if mount is None or mount.fstype != "zfs" or mount.mountpoint != real_dst:
            return None
        return _measure_with_zfs(mount.source)


def _measure_with_btrfs(real_dst: str) -> Optional[MeasureResult]:
    """Run `btrfs filesystem du -s` for a subvolume root and return its size."""
    btrfs_binary = shutil.which("btrfs")
    if btrfs_binary is None:
        log.warning("btrfs binary not found; cannot measure %s", real_dst)
        return None
    try:
        completed = subprocess.run(
            [btrfs_binary, "filesystem", "du", "-s", "--raw", "--", real_dst],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("btrfs filesystem du failed for %s: %s", real_dst, exc)
        return None
    if completed.returncode != 0:
        log.warning(
            "btrfs filesystem du exited %d for %s: %s",
            completed.returncode, real_dst, completed.stderr.strip(),
        )
        return None
    data_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(data_lines) < 2:
        log.warning("Unexpected btrfs filesystem du output for %s", real_dst)
        return None
    try:
        used_bytes = int(data_lines[1].split()[0])
    except (IndexError, ValueError) as exc:
        log.warning("Failed to parse btrfs filesystem du output for %s: %s", real_dst, exc)
        return None
    return MeasureResult(bytes=used_bytes, file_count=None, source="btrfs")


class BtrfsProvider(Provider):
    """Measures a repo that is a Btrfs subvolume root (qgroup usage)."""

    name = "btrfs"

    def applicable(self, real_dst: str) -> bool:
        if shutil.which("btrfs") is None:
            return False
        mount = _get_mount_for_dst(real_dst)
        return mount is not None and mount.fstype == "btrfs" and mount.mountpoint == real_dst

    def measure(self, real_dst: str) -> Optional[MeasureResult]:
        mount = _get_mount_for_dst(real_dst)
        if mount is None or mount.fstype != "btrfs" or mount.mountpoint != real_dst:
            return None
        return _measure_with_btrfs(real_dst)


def _find_xfs_project_id(real_dst: str) -> Optional[str]:
    """Look up an XFS project id configured for real_dst in the trusted projects table."""
    try:
        with open(_XFS_PROJECTS_PATH, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        project_id, path = parts[0].strip(), parts[1].strip()
        if path == real_dst:
            return project_id
    return None


def _measure_with_xfs_quota(mountpoint: str, project_id: str) -> Optional[MeasureResult]:
    """Run `xfs_quota report -p` for a trusted project id and return its usage."""
    xfs_quota_binary = shutil.which("xfs_quota")
    if xfs_quota_binary is None:
        log.warning("xfs_quota binary not found; cannot measure project %s", project_id)
        return None
    try:
        completed = subprocess.run(
            [xfs_quota_binary, "-x", "-c", "report -p -n -N", mountpoint],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("xfs_quota report failed for %s: %s", mountpoint, exc)
        return None
    if completed.returncode != 0:
        log.warning(
            "xfs_quota report exited %d for %s: %s",
            completed.returncode, mountpoint, completed.stderr.strip(),
        )
        return None
    project_marker = f"#{project_id}"
    for line in completed.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] not in (project_marker, project_id):
            continue
        if len(fields) < 2:
            log.warning("Unexpected xfs_quota report line for project %s", project_id)
            return None
        try:
            used_blocks = int(fields[1])
        except ValueError as exc:
            log.warning("Failed to parse xfs_quota report line for project %s: %s", project_id, exc)
            return None
        return MeasureResult(bytes=used_blocks * 1024, file_count=None, source="xfs-quota")
    log.warning("Project %s not found in xfs_quota report for %s", project_id, mountpoint)
    return None


class XfsQuotaProvider(Provider):
    """Measures a repo backed by a configured XFS project quota."""

    name = "xfs-quota"

    def applicable(self, real_dst: str) -> bool:
        if shutil.which("xfs_quota") is None:
            return False
        mount = _get_mount_for_dst(real_dst)
        if mount is None or mount.fstype != "xfs":
            return False
        return _find_xfs_project_id(real_dst) is not None

    def measure(self, real_dst: str) -> Optional[MeasureResult]:
        mount = _get_mount_for_dst(real_dst)
        if mount is None or mount.fstype != "xfs":
            return None
        project_id = _find_xfs_project_id(real_dst)
        if project_id is None:
            return None
        return _measure_with_xfs_quota(mount.mountpoint, project_id)


def _measure_with_du_binary(real_dst: str) -> Optional[MeasureResult]:
    """Run `du -s -B1` for real_dst and return its allocated size, or None on failure."""
    du_binary = shutil.which("du")
    if du_binary is None:
        return None
    try:
        completed = subprocess.run(
            [du_binary, "-s", "-B1", "--", real_dst],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("du failed for %s: %s", real_dst, exc)
        return None
    if completed.returncode != 0:
        log.warning(
            "du exited %d for %s: %s", completed.returncode, real_dst, completed.stderr.strip(),
        )
        return None
    try:
        size_bytes = int(completed.stdout.split()[0])
    except (IndexError, ValueError) as exc:
        log.warning("Failed to parse du output for %s: %s", real_dst, exc)
        return None
    return MeasureResult(bytes=size_bytes, file_count=None, source="du")


def _scan_directory_size(root: str) -> tuple[int, int]:
    """Recursively sum allocated bytes and count entries under root.

    Uses os.scandir(follow_symlinks=False) so symlinked directories are never
    recursed into (they are counted as a plain entry instead). Hardlinked
    files are counted once via a (st_dev, st_ino) dedup set.
    """
    seen_inodes: set[tuple[int, int]] = set()
    total_bytes = 0
    file_count = 0
    pending_dirs = [root]
    while pending_dirs:
        current_dir = pending_dirs.pop()
        directory_stat = os.stat(current_dir, follow_symlinks=False)
        total_bytes += directory_stat.st_blocks * 512
        with os.scandir(current_dir) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending_dirs.append(entry.path)
                    continue
                stat_result = entry.stat(follow_symlinks=False)
                if stat_result.st_nlink > 1:
                    inode_key = (stat_result.st_dev, stat_result.st_ino)
                    if inode_key in seen_inodes:
                        continue
                    seen_inodes.add(inode_key)
                total_bytes += stat_result.st_blocks * 512
                file_count += 1
    return total_bytes, file_count


def _measure_with_python_walk(real_dst: str) -> Optional[MeasureResult]:
    """Pure-python fallback measurement when the `du` binary is unavailable/fails."""
    try:
        total_bytes, file_count = _scan_directory_size(real_dst)
    except OSError as exc:
        log.warning("Python fallback walk failed for %s: %s", real_dst, exc)
        return None
    return MeasureResult(bytes=total_bytes, file_count=file_count, source="du-python")


class DuProvider(Provider):
    """Always-applicable fallback: `du -s -B1` (allocated bytes, dedups hardlinks).

    If the `du` binary is unavailable, falls back to a pure-python walk using
    os.scandir(follow_symlinks=False) that does NOT follow symlinks and dedups
    hardlinks via a set of (st_dev, st_ino) before summing st_blocks*512; that
    result carries source "du-python".
    """

    name = "du"

    def applicable(self, real_dst: str) -> bool:
        return True

    def measure(self, real_dst: str) -> Optional[MeasureResult]:
        result = _measure_with_du_binary(real_dst)
        if result is not None:
            return result
        return _measure_with_python_walk(real_dst)
