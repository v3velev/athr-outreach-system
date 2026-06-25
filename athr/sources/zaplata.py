"""zaplata.bg source - the primary backbone.

Why it's primary: no Cloudflare (runs from any host), every ad carries a
schema.org JSON-LD JobPosting (clean structured data), exact post dates, and
the employer website ~40% of the time.

Crawl strategy: for each configured sector, walk pages newest-first; for each
job URL, if we've seen the id we stop that sector (everything below is older);
otherwise fetch the detail page and parse its JSON-LD.

KNOWN QUIRK handled here: the JSON-LD baseSalary.currency says "BGN" but the
actual amount is EUR (Bulgaria adopts the euro Jan 2026; the structured data
lags). We store the number and force currency = EUR.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from ..models import Job
from .base import JobSource, log

JOB_URL_RE = re.compile(
    r"https://www\.zaplata\.bg/([a-z0-9\-]+)/([a-z0-9\-]+)/(\d{5,})/([a-z0-9\-]+)"
)
BASE = "https://www.zaplata.bg"


class ZaplataSource(JobSource):
    name = "zaplata"

    def fetch_new_jobs(self, max_new: int) -> Iterator[Job]:
        sectors = self.config.get("sources", "zaplata", "sectors", default=[])
        max_pages = self.config.get("sources", "zaplata", "max_pages_per_sector", default=5)
        cutoff = self._first_run_cutoff()

        emitted = 0
        for sector in sectors:
            if emitted >= max_new:
                break
            for job in self._crawl_sector(sector, max_pages):
                if emitted >= max_new:
                    break
                # first-run safety: skip very old postings. Mark them seen so
                # they stay skipped forever (otherwise day-2, once the cutoff
                # lifts, we'd suddenly email weeks-old jobs).
                if cutoff and job.date_posted and job.date_posted < cutoff:
                    self.store.mark_seen(job.uid, job.source, job.external_id,
                                         job.company_name)
                    continue
                emitted += 1
                yield job

    # ---- internals ----
    def _first_run_cutoff(self) -> Optional[str]:
        if self.store.seen_count() > 0:
            return None  # not the first run; the seen-DB handles freshness
        days = self.config.get("run", "first_run_max_age_days", default=3)
        return (date.today() - timedelta(days=days)).isoformat()

    def _crawl_sector(self, sector: str, max_pages: int) -> Iterator[Job]:
        seen_ids_this_sector: set[str] = set()
        for page in range(1, max_pages + 1):
            url = f"{BASE}/{sector}/" if page == 1 else f"{BASE}/{sector}/?page={page}"
            html = self.http.get(url)
            if not html:
                break
            matches = JOB_URL_RE.findall(html)
            if not matches:
                break
            # dedup within the page, preserve order (newest-first)
            page_ids = []
            for sec, city, jid, slug in matches:
                if jid in seen_ids_this_sector:
                    continue
                seen_ids_this_sector.add(jid)
                page_ids.append((sec, city, jid, slug))
            if not page_ids:
                break  # page fully repeated -> end of sector

            # Count NEW (previously-unseen) jobs at scrape time. We must decide
            # this BEFORE yielding, because the pipeline marks each yielded job
            # as seen immediately - so re-checking is_seen() afterwards would
            # always report "seen" and falsely stop the crawl after page 1.
            new_on_page = 0
            for sec, city, jid, slug in page_ids:
                if self.store.is_seen(f"{self.name}:{jid}"):
                    continue  # already processed in a prior run
                new_on_page += 1
                job = self._fetch_detail(f"{BASE}/{sec}/{city}/{jid}/{slug}/", sector)
                if job:
                    yield job
            # A page with zero new jobs means we've caught up to last run; stop.
            if new_on_page == 0:
                log.info("  [zaplata/%s] page %d had no new jobs, stopping", sector, page)
                break

    def _fetch_detail(self, url: str, sector: str) -> Optional[Job]:
        html = self.http.get(url)
        if not html:
            return None
        ld = self._extract_jsonld(html)
        m = JOB_URL_RE.match(url)
        ext_id = m.group(3) if m else url.rstrip("/").split("/")[-2]
        city_slug = m.group(2) if m else ""

        if not ld:
            # Fallback: minimal job from URL if JSON-LD missing (rare on zaplata).
            return Job(source=self.name, external_id=ext_id, url=url,
                       title="", company_name="", sector=sector, city=city_slug)

        org = ld.get("hiringOrganization") or {}
        if isinstance(org, str):
            org = {"name": org}
        loc = ld.get("jobLocation") or {}
        addr = loc.get("address", {}) if isinstance(loc, dict) else {}
        sal = ld.get("baseSalary") or {}
        val = sal.get("value", {}) if isinstance(sal, dict) else {}

        return Job(
            source=self.name,
            external_id=ext_id,
            url=url,
            title=(ld.get("title") or "").strip(),
            company_name=(org.get("name") or "").strip(),
            sector=sector,
            city=addr.get("addressLocality") or city_slug,
            country=addr.get("addressCountry") or "BG",
            salary_min=_num(val.get("minValue")),
            salary_max=_num(val.get("maxValue")),
            salary_currency="EUR",   # force EUR: zaplata mislabels EUR as BGN
            company_website=_pick_website(org.get("url"), org.get("sameAs")),
            description=_clean_html(ld.get("description") or ""),
            date_posted=(ld.get("datePosted") or "")[:10],
        )

    @staticmethod
    def _extract_jsonld(html: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            if not tag.string:
                continue
            try:
                data = json.loads(tag.string)
            except (json.JSONDecodeError, TypeError):
                continue
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
        return None


_SOCIAL = ("linkedin.", "facebook.", "instagram.", "twitter.", "x.com",
           "youtube.", "tiktok.", "zaplata.bg")


def _pick_website(*candidates) -> str:
    """Return the first candidate that looks like a real company website,
    skipping social/profile URLs (which JSON-LD often puts in sameAs)."""
    for c in candidates:
        c = (c or "").strip()
        if c and not any(s in c.lower() for s in _SOCIAL):
            return c
    return ""


def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _clean_html(text: str) -> str:
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
