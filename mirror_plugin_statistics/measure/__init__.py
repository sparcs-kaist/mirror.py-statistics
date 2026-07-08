"""Measurement entry point: pick a provider and measure a repository directory."""

from __future__ import annotations

import logging
import os
from typing import Optional

from .providers import (
    BtrfsProvider,
    DuProvider,
    MeasureResult,
    Provider,
    XfsQuotaProvider,
    ZfsProvider,
)

log = logging.getLogger("mirror")

# Maps a config provider name to its Provider class.
PROVIDER_REGISTRY: dict[str, type[Provider]] = {
    ZfsProvider.name: ZfsProvider,
    BtrfsProvider.name: BtrfsProvider,
    XfsQuotaProvider.name: XfsQuotaProvider,
    DuProvider.name: DuProvider,
}


def measure_usage(dst: str, providers: list[str]) -> Optional[MeasureResult]:
    """Measure the disk usage of a repository destination directory.

    Resolves ``dst`` to a real path and validates it is an existing directory.
    Then tries each provider named in ``providers`` (in order); the first whose
    ``applicable()`` is true and whose ``measure()`` returns a result wins. If
    none apply/succeed, the ``du`` provider is used as the always-applicable
    fallback (even if not listed).

    Args:
        dst(str): The repository's on-disk path (package.settings.dst).
        providers(list[str]): Provider names in priority order.

    Return:
        result(Optional[MeasureResult]): The measurement, or None if dst is
            missing/not a directory or every provider (incl. du) failed.
    """
    real_dst = os.path.realpath(dst)
    if not os.path.isdir(real_dst):
        return None

    for name in providers:
        provider_cls = PROVIDER_REGISTRY.get(name)
        if provider_cls is None:
            log.warning("Unknown measurement provider %r; skipping", name)
            continue
        provider = provider_cls()
        try:
            if provider.applicable(real_dst):
                result = provider.measure(real_dst)
                if result is not None:
                    return result
        except Exception as exc:
            log.warning("Provider %r raised while measuring %s: %s", name, real_dst, exc)

    try:
        return DuProvider().measure(real_dst)
    except Exception as exc:
        log.warning("du fallback raised while measuring %s: %s", real_dst, exc)
        return None
