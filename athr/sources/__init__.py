"""Job sources. Each yields Job objects; the pipeline treats them uniformly."""

from .base import JobSource
from .zaplata import ZaplataSource
from .jobsbg import JobsBgSource
from .jobtiger import JobTigerSource


def build_sources(config, http, store):
    """Instantiate the enabled sources from config."""
    sources = []
    if config.get("sources", "zaplata", "enabled", default=False):
        sources.append(ZaplataSource(config, http, store))
    if config.get("sources", "jobsbg", "enabled", default=False):
        sources.append(JobsBgSource(config, http, store))
    if config.get("sources", "jobtiger", "enabled", default=False):
        sources.append(JobTigerSource(config, http, store))
    return sources


__all__ = ["JobSource", "ZaplataSource", "JobsBgSource", "JobTigerSource", "build_sources"]
