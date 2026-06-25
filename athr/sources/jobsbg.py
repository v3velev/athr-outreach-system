"""jobs.bg source - high-volume BONUS (disabled by default).

jobs.bg sits behind a Cloudflare managed challenge. curl_cffi's Chrome TLS
impersonation gets through on the SEO *category* pages (e.g. /hospitality) -
but NOT the dynamic front_job_search.php endpoint, which we don't need.

Caveats (why it's a bonus, not the backbone):
  * Soft rate-limits a hammering IP -> we pace hard (config request_delay).
  * Datacenter IPs (a VPS) are trusted less than residential; if a VPS gets
    blocked, leave this disabled and rely on zaplata.
  * Listing pages give title/company/city but NOT the employer website.

We parse the category listing pages only (cheap, 20 ads each, paginated).
"""

from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from ..models import Job
from .base import JobSource, log

BASE = "https://www.jobs.bg"
JOB_ID_RE = re.compile(r"/job/(\d+)")
# Bulgarian legal-form tokens mark the end of an employer name in the card text.
COMPANY_RE = re.compile(r"([A-ZА-Я][^;]{2,60}?(?:ЕООД|ООД|АД|ЕАД|ЕТ))\b")


class JobsBgSource(JobSource):
    name = "jobsbg"

    def fetch_new_jobs(self, max_new: int) -> Iterator[Job]:
        cats = self.config.get("sources", "jobsbg", "categories", default=[])
        max_pages = self.config.get("sources", "jobsbg", "max_pages_per_category", default=3)
        emitted = 0
        for cat in cats:
            if emitted >= max_new:
                break
            for job in self._crawl_category(cat, max_pages):
                if emitted >= max_new:
                    break
                emitted += 1
                yield job

    def _crawl_category(self, cat: str, max_pages: int) -> Iterator[Job]:
        for page in range(1, max_pages + 1):
            url = f"{BASE}/{cat}" if page == 1 else f"{BASE}/{cat}?page={page}"
            html = self.http.get(url)   # paced via HttpClient.limiter
            if not html:
                break
            cards = self._parse_cards(html, cat)
            if not cards:
                break
            new_this_page = 0
            for job in cards:
                if self.store.is_seen(job.uid):
                    continue
                new_this_page += 1
                yield job
            if new_this_page == 0:
                log.info("  [jobsbg/%s] page %d all seen, stopping", cat, page)
                break

    def _parse_cards(self, html: str, cat: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        for a in soup.select("a[href*='/job/']"):
            m = JOB_ID_RE.search(a.get("href", ""))
            if not m:
                continue
            jid = m.group(1)
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            # climb to the card container to read company + city
            block = a
            for _ in range(5):
                if block.parent:
                    block = block.parent
            text = block.get_text(" ", strip=True)
            comp = COMPANY_RE.search(text)
            title = a.get_text(" ", strip=True)
            jobs.append(Job(
                source=self.name,
                external_id=jid,
                url=f"{BASE}/job/{jid}",
                title=_clean_title(title),
                company_name=comp.group(1).strip() if comp else "",
                sector=cat,
                city=_guess_city(text),
                country="BG",
            ))
        return jobs


def _clean_title(t: str) -> str:
    # strip the leading star-rating glyphs jobs.bg injects
    return re.sub(r"^(star)+", "", t).strip()[:120]


def _guess_city(text: str) -> str:
    for city in ("София", "Пловдив", "Варна", "Бургас", "Русе", "Стара Загора",
                 "Плевен", "Велико Търново", "Банско", "Перник"):
        if city in text:
            return city
    return ""
