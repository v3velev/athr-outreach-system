"""jobtiger.bg source - supplementary coverage (disabled by default).

jobtiger is server-rendered with employer names and no Cloudflare, but its
markup is messier than zaplata's and it lacks JSON-LD. This is a minimal
listing scraper kept as an extension point; enable + extend if/when the client
wants more volume beyond zaplata + jobs.bg.
"""

from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from ..models import Job
from .base import JobSource

BASE = "https://www.jobtiger.bg"
COMPANY_RE = re.compile(r"([A-ZА-Я][^\n]{2,60}?(?:ЕООД|ООД|АД|ЕАД|ЕТ))\b")


class JobTigerSource(JobSource):
    name = "jobtiger"

    def fetch_new_jobs(self, max_new: int) -> Iterator[Job]:
        # Minimal implementation: pull the main listing, emit unseen ads.
        html = self.http.get(f"{BASE}/obiavi-za-rabota/")
        if not html:
            return
        soup = BeautifulSoup(html, "html.parser")
        emitted = 0
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            if emitted >= max_new:
                break
            m = re.search(r"/(obiava|ad|job)[/-](\d{4,})", a["href"])
            if not m:
                continue
            jid = m.group(2)
            if jid in seen:
                continue
            seen.add(jid)
            uid = f"{self.name}:{jid}"
            if self.store.is_seen(uid):
                continue
            block = a
            for _ in range(4):
                if block.parent:
                    block = block.parent
            text = block.get_text(" ", strip=True)
            comp = COMPANY_RE.search(text)
            url = a["href"] if a["href"].startswith("http") else BASE + a["href"]
            emitted += 1
            yield Job(
                source=self.name,
                external_id=jid,
                url=url,
                title=a.get_text(" ", strip=True)[:120],
                company_name=comp.group(1).strip() if comp else "",
                sector="",
                country="BG",
            )
