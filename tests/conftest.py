"""Shared pytest fixtures for the statistics plug-in tests.

Mirrors the plug-in-state snapshot/restore pattern from mirror.py's own
tests/test_plugin_status_fields.py so registering the statistics plug-in in one
test never leaks into another.
"""

from __future__ import annotations

import pytest

import mirror
import mirror.plugin
import mirror.sync
from mirror.structure import Package, PackageSettings


@pytest.fixture(autouse=True)
def _restore_plugin_state():
    """Snapshot every plug-in registry/hook store and restore it after each test."""
    orig_stat_hooks = list(mirror.plugin._status_stat_hooks)
    orig_web_hooks = list(mirror.plugin._status_web_hooks)
    orig_registry = dict(mirror.plugin._registry)
    orig_methods = list(mirror.sync.methods)
    orig_stat_transform_owner = mirror.plugin._stat_transform_owner
    orig_web_status_transform_owner = mirror.plugin._web_status_transform_owner
    orig_status_outputs = dict(mirror.plugin._status_outputs)
    yield
    mirror.plugin._status_stat_hooks[:] = orig_stat_hooks
    mirror.plugin._status_web_hooks[:] = orig_web_hooks
    mirror.plugin._registry.clear()
    mirror.plugin._registry.update(orig_registry)
    mirror.sync.methods[:] = orig_methods
    mirror.plugin._stat_transform_owner = orig_stat_transform_owner
    mirror.plugin._web_status_transform_owner = orig_web_status_transform_owner
    mirror.plugin._status_outputs.clear()
    mirror.plugin._status_outputs.update(orig_status_outputs)


def make_package(pkgid: str = "pkg1", dst: str = "/srv/ftp/pkg1", status: str = "ACTIVE") -> Package:
    """Build a minimal Package for tests.

    Args:
        pkgid(str): Package id.
        dst(str): On-disk destination path.
        status(str): Initial status.

    Return:
        package(Package): A constructed Package instance.
    """
    settings = PackageSettings(hidden=False, src="rsync://example.com/pkg1", dst=dst, options={})
    return Package(
        pkgid=pkgid,
        name=pkgid,
        status=status,
        href=f"/{pkgid}",
        synctype="rsync",
        syncrate=3600,
        link=[],
        settings=settings,
    )
