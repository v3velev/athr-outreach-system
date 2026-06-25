"""Core data structures passed between pipeline stages.

One Job flows through the pipeline and accumulates a Contact and a Message.
Everything is a plain dataclass so it serialises cleanly to the DB / CSV.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Job:
    """A single job posting scraped from a board."""
    source: str                 # "zaplata" | "jobsbg" | "jobtiger"
    external_id: str            # the board's own job id (sequential)
    url: str
    title: str
    company_name: str
    sector: str = ""            # board sector/category slug
    city: str = ""
    country: str = "BG"         # ISO-ish code from JSON-LD (BG, NL, ...)
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: str = "EUR"   # NOTE: zaplata JSON-LD mislabels EUR as BGN; we correct to EUR
    company_website: str = ""   # present ~40% of the time on zaplata
    description: str = ""
    date_posted: str = ""       # ISO date "2026-06-22"

    @property
    def uid(self) -> str:
        """Globally unique job id across sources."""
        return f"{self.source}:{self.external_id}"


@dataclass
class Contact:
    """A decision-maker found at the hiring company (via enrichment)."""
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    job_title: str = ""
    email: str = ""
    email_status: str = ""      # "VERIFIED" | "RISKY" | "MOCK" ...
    linkedin_url: str = ""
    phone: str = ""
    source: str = ""            # "prospeo" | "mock" | "website"

    @property
    def is_usable(self) -> bool:
        """Basic structural email sanity (string ops, not regex parsing):
        exactly one '@', a non-empty local part, and a dotted domain."""
        e = (self.email or "").strip()
        if e.count("@") != 1:
            return False
        local, _, domain = e.partition("@")
        return bool(local) and "." in domain and not domain.startswith(".") \
            and not domain.endswith(".") and " " not in e


@dataclass
class Message:
    """The generated outreach email."""
    subject: str = ""
    body: str = ""
    angle: str = ""             # which sector pitch angle was used
    variant: str = "A"
    generator: str = ""         # "anthropic" | "mock"


@dataclass
class OutreachItem:
    """A job that made it through the whole pipeline (or was dropped)."""
    job: Job
    contact: Optional[Contact] = None
    message: Optional[Message] = None
    status: str = "new"         # new|filtered|enriched|generated|sent|skipped|error
    drop_reason: str = ""       # why it was filtered/skipped, if so
    sent_ok: bool = False
    notes: str = ""

    def flat(self) -> dict:
        """Flatten to a single dict for CSV / DB writing."""
        row = {f"job_{k}": v for k, v in asdict(self.job).items()}
        row.update({f"contact_{k}": v for k, v in asdict(self.contact).items()} if self.contact else {})
        row.update({f"msg_{k}": v for k, v in asdict(self.message).items()} if self.message else {})
        row["status"] = self.status
        row["drop_reason"] = self.drop_reason
        row["sent_ok"] = self.sent_ok
        row["notes"] = self.notes
        return row


@dataclass
class RunStats:
    """Counters for the end-of-run summary."""
    started_at: str = ""
    finished_at: str = ""
    scraped: int = 0
    new_jobs: int = 0
    after_icp: int = 0
    after_dedup: int = 0
    enriched: int = 0
    generated: int = 0
    sent: int = 0
    errors: int = 0
    drop_reasons: dict = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1
