"""Unit tests for mirror_plugin_statistics.measure and .measure.providers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from mirror_plugin_statistics.measure import measure_usage
from mirror_plugin_statistics.measure.providers import (
    MountInfo,
    _scan_directory_size,
    find_mount_for_path,
    read_mounts,
)


# ---------------------------------------------------------------------------
# du-python fallback (pure-python directory walk)
# ---------------------------------------------------------------------------

def _build_tree_with_hardlink_and_symlink(tmp_path: Path) -> tuple[Path, int, int]:
    """Create a tree with a hardlink and a symlinked directory outside it.

    Return:
        info(tuple[Path, int, int]): (root dir, expected total bytes, expected
            file count) computed independently from the created entries.
    """
    root = tmp_path / "root"
    root.mkdir()
    subdir = root / "subdir"
    subdir.mkdir()

    file_a = root / "file_a.txt"
    file_a.write_bytes(b"a" * 100)
    file_b = subdir / "file_b.txt"
    file_b.write_bytes(b"b" * 200)

    hardlink_to_a = root / "hardlink_to_a.txt"
    os.link(file_a, hardlink_to_a)

    external_dir = tmp_path / "external_dir"
    external_dir.mkdir()
    extra_file = external_dir / "extra_file.txt"
    extra_file.write_bytes(b"x" * 50_000)

    link_to_external = root / "link_to_external"
    link_to_external.symlink_to(external_dir, target_is_directory=True)

    expected_bytes = (
        os.lstat(file_a).st_blocks * 512
        + os.lstat(file_b).st_blocks * 512
        + os.lstat(link_to_external).st_blocks * 512
    )
    expected_file_count = 3
    return root, expected_bytes, expected_file_count


def test_scan_directory_size_dedups_hardlinks_and_skips_symlinked_dirs(tmp_path: Path) -> None:
    root, expected_bytes, expected_file_count = _build_tree_with_hardlink_and_symlink(tmp_path)

    total_bytes, file_count = _scan_directory_size(str(root))

    assert total_bytes == expected_bytes
    assert file_count == expected_file_count


def test_du_provider_python_fallback_used_when_du_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    root, expected_bytes, expected_file_count = _build_tree_with_hardlink_and_symlink(tmp_path)
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)

    result = providers.DuProvider().measure(str(root))

    assert result is not None
    assert result.source == "du-python"
    assert result.bytes == expected_bytes
    assert result.file_count == expected_file_count


# ---------------------------------------------------------------------------
# read_mounts / find_mount_for_path
# ---------------------------------------------------------------------------

_FAKE_PROC_MOUNTS = (
    "proc /proc proc rw,nosuid,nodev,noexec 0 0\n"
    "rpool/ROOT / zfs rw,relatime,xattr,noacl 0 0\n"
    "tank/repos /srv/mirror\\040data zfs rw,relatime,xattr,noacl 0 0\n"
    "tank/repos/pkg1 /srv/mirror\\040data/pkg1 zfs rw,relatime,xattr,noacl 0 0\n"
)


def test_read_mounts_parses_fields_and_unescapes_octal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    fake_mounts_file = tmp_path / "mounts"
    fake_mounts_file.write_text(_FAKE_PROC_MOUNTS)
    monkeypatch.setattr(providers, "_PROC_MOUNTS_PATH", str(fake_mounts_file))

    mounts = read_mounts()

    assert MountInfo(mountpoint="/proc", fstype="proc", source="proc") in mounts
    assert any(m.mountpoint == "/srv/mirror data" and m.fstype == "zfs" for m in mounts)
    assert any(
        m.mountpoint == "/srv/mirror data/pkg1" and m.source == "tank/repos/pkg1"
        for m in mounts
    )


def test_read_mounts_returns_empty_list_on_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mirror_plugin_statistics.measure import providers

    monkeypatch.setattr(providers, "_PROC_MOUNTS_PATH", "/nonexistent/path/mounts")

    assert read_mounts() == []


def test_find_mount_for_path_picks_longest_matching_prefix() -> None:
    mounts = [
        MountInfo(mountpoint="/", fstype="ext4", source="/dev/sda1"),
        MountInfo(mountpoint="/srv/mirror data", fstype="zfs", source="tank/repos"),
        MountInfo(mountpoint="/srv/mirror data/pkg1", fstype="zfs", source="tank/repos/pkg1"),
    ]

    match = find_mount_for_path("/srv/mirror data/pkg1", mounts)
    assert match is not None
    assert match.source == "tank/repos/pkg1"

    match = find_mount_for_path("/srv/mirror data/pkg2", mounts)
    assert match is not None
    assert match.source == "tank/repos"


def test_find_mount_for_path_respects_path_boundary() -> None:
    mounts = [MountInfo(mountpoint="/srv", fstype="ext4", source="/dev/sda1")]

    assert find_mount_for_path("/srvfoo", mounts) is None
    assert find_mount_for_path("/srv/foo", mounts) is not None
    assert find_mount_for_path("/srv", mounts) is not None


def test_find_mount_for_path_returns_none_when_no_match() -> None:
    assert find_mount_for_path("/some/path", []) is None


# ---------------------------------------------------------------------------
# measure_usage dispatcher
# ---------------------------------------------------------------------------

def test_measure_usage_returns_none_for_missing_path() -> None:
    assert measure_usage("/definitely/not/a/real/path", ["du"]) is None


def test_measure_usage_uses_du_binary_with_expected_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    captured_commands: list[list[str]] = []

    def fake_which(name: str) -> Optional[str]:
        return "/usr/bin/du" if name == "du" else None

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured_commands.append(command)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=f"12345\t{tmp_path}\n", stderr=""
        )

    monkeypatch.setattr(providers.shutil, "which", fake_which)
    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    result = measure_usage(str(tmp_path), ["du"])

    assert result is not None
    assert result.bytes == 12345
    assert result.source == "du"
    assert captured_commands, "subprocess.run was never called"
    used_command = captured_commands[0]
    assert used_command[0] == "/usr/bin/du"
    assert "-B1" in used_command
    assert "--" in used_command
    assert used_command[-1] == str(Path(tmp_path).resolve())


def test_measure_usage_skips_unknown_provider_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    monkeypatch.setattr(providers.shutil, "which", lambda name: None)

    result = measure_usage(str(tmp_path), ["not-a-real-provider"])

    assert result is not None
    assert result.source == "du-python"


# ---------------------------------------------------------------------------
# Native providers (zfs / btrfs / xfs-quota) selection + fallback
# ---------------------------------------------------------------------------

def _install_fake_mounts(
    monkeypatch: pytest.MonkeyPatch, mounts_text: str
) -> None:
    """Point providers._PROC_MOUNTS_PATH at an in-memory mounts table written to a temp file."""
    import tempfile

    from mirror_plugin_statistics.measure import providers

    handle = tempfile.NamedTemporaryFile("w", suffix=".mounts", delete=False)
    handle.write(mounts_text)
    handle.close()
    monkeypatch.setattr(providers, "_PROC_MOUNTS_PATH", handle.name)


def _fake_run_router(routes: dict) -> object:
    """Build a fake subprocess.run that dispatches by the binary basename.

    Args:
        routes(dict): binary-basename -> either a CompletedProcess-producing
            callable(command) or an Exception instance to raise.
    """

    def fake_run(command: list, **kwargs: object) -> subprocess.CompletedProcess:
        binary = os.path.basename(command[0])
        route = routes.get(binary)
        if route is None:
            raise AssertionError(f"unexpected subprocess call: {command}")
        if isinstance(route, BaseException):
            raise route
        return route(command)

    return fake_run


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> object:
    def _make(command: list) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _make


def test_measure_usage_selects_zfs_for_dataset_mountpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    real = os.path.realpath(str(tmp_path))
    _install_fake_mounts(monkeypatch, f"tank/data {real} zfs rw 0 0\n")
    monkeypatch.setattr(providers.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(
        providers.subprocess, "run", _fake_run_router({"zfs": _completed("4096\n")})
    )

    result = measure_usage(str(tmp_path), ["zfs", "du"])

    assert result is not None
    assert result.source == "zfs"
    assert result.bytes == 4096


def test_measure_usage_falls_back_to_du_when_zfs_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    real = os.path.realpath(str(tmp_path))
    _install_fake_mounts(monkeypatch, f"tank/data {real} zfs rw 0 0\n")
    monkeypatch.setattr(providers.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(
        providers.subprocess,
        "run",
        _fake_run_router(
            {
                "zfs": _completed("", returncode=1, stderr="boom"),
                "du": _completed("777\t" + real + "\n"),
            }
        ),
    )

    result = measure_usage(str(tmp_path), ["zfs", "du"])

    assert result is not None
    assert result.source == "du"
    assert result.bytes == 777


def test_measure_usage_falls_back_to_du_on_zfs_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    real = os.path.realpath(str(tmp_path))
    _install_fake_mounts(monkeypatch, f"tank/data {real} zfs rw 0 0\n")
    monkeypatch.setattr(providers.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(
        providers.subprocess,
        "run",
        _fake_run_router(
            {
                "zfs": subprocess.TimeoutExpired(cmd="zfs", timeout=1),
                "du": _completed("888\t" + real + "\n"),
            }
        ),
    )

    result = measure_usage(str(tmp_path), ["zfs", "du"])

    assert result is not None
    assert result.source == "du"
    assert result.bytes == 888


def test_measure_usage_selects_btrfs_for_subvolume_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    real = os.path.realpath(str(tmp_path))
    _install_fake_mounts(monkeypatch, f"/dev/sdb {real} btrfs rw 0 0\n")
    monkeypatch.setattr(providers.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(
        providers.subprocess,
        "run",
        _fake_run_router(
            {"btrfs": _completed("Total Exclusive Filename\n2048 2048 " + real + "\n")}
        ),
    )

    result = measure_usage(str(tmp_path), ["btrfs", "du"])

    assert result is not None
    assert result.source == "btrfs"
    assert result.bytes == 2048


def test_measure_usage_selects_xfs_quota_for_configured_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mirror_plugin_statistics.measure import providers

    real = os.path.realpath(str(tmp_path))
    _install_fake_mounts(monkeypatch, f"/dev/sdc {real} xfs rw 0 0\n")

    projects_file = tmp_path / "projects"
    projects_file.write_text(f"77:{real}\n")
    monkeypatch.setattr(providers, "_XFS_PROJECTS_PATH", str(projects_file))
    monkeypatch.setattr(providers.shutil, "which", lambda name: f"/usr/sbin/{name}")
    # xfs_quota report -p -N: "#<proj> <blocks(KiB)> ..." -> bytes = blocks * 1024.
    monkeypatch.setattr(
        providers.subprocess,
        "run",
        _fake_run_router({"xfs_quota": _completed("#77 3072 0 0 00 [------]\n")}),
    )

    result = measure_usage(str(tmp_path), ["xfs-quota", "du"])

    assert result is not None
    assert result.source == "xfs-quota"
    assert result.bytes == 3072 * 1024


def test_measure_usage_skips_zfs_when_mount_is_not_dataset_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subdirectory of a zfs dataset (mountpoint != real_dst) must not use zfs."""
    from mirror_plugin_statistics.measure import providers

    real = os.path.realpath(str(tmp_path))
    parent = os.path.dirname(real)
    _install_fake_mounts(monkeypatch, f"tank/data {parent} zfs rw 0 0\n")

    def which(name: str):
        return "/usr/bin/du" if name == "du" else f"/usr/sbin/{name}"

    monkeypatch.setattr(providers.shutil, "which", which)
    monkeypatch.setattr(
        providers.subprocess, "run", _fake_run_router({"du": _completed("55\t" + real + "\n")})
    )

    result = measure_usage(str(tmp_path), ["zfs", "du"])

    assert result is not None
    assert result.source == "du"
    assert result.bytes == 55
