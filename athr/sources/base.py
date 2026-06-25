"""Base class for all job sources.

A source knows how to crawl one board and yield NEW jobs (ones not already in
the seen-jobs DB). It stops paginating a section as soon as it hits jobs it has
already seen - that is the incremental "what's new since last run" mechanism.
"""

from __future__ import annotations

from typing import Iterator

from ..models import Job
from ..utils import get_logger

log = get_logger()


class JobSource:
    name = "base"

    def __init__(self, config, http, store):
        self.config = config
        self.http = http
        self.store = store

    def fetch_new_jobs(self, max_new: int) -> Iterator[Job]:
        """Yield up to `max_new` jobs not yet seen. Subclasses implement."""
        raise NotImplementedError

    # helper shared by subclasses
    def _is_new(self, job: Job) -> bool:
        return not self.store.is_seen(job.uid)
