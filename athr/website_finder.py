"""Find CONTACTS (first name + email) for a company in one go.

Per the agreed flow: when Prospeo finds nobody and the posting has no website to
scrape, ask the Parallel Search API to find up to 3 people-and-emails for the
company in a single call, each linking an email to a person's FIRST name (last
name not required). Found emails are then Trykitt-verified downstream.

Returns up to 3 {first_name, email} dicts. 0 is fine, 1 is fine; never more than 3.

"For now": until PARALLEL_API_KEY is set, runs CACHE-ONLY from
data/found_contacts.json (which Claude Code / an operator fills), and logs every
unresolved company to data/contact_lookups_needed.csv. Same interface either way.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .utils import get_logger

log = get_logger()

PARALLEL_URL = "https://api.parallel.ai/v1/search"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# never treat these as a real contact address
JUNK_EMAIL = ("noreply", "no-reply", "donotreply", "postmaster", "abuse",
              "sentry", "example", "wixpress", "godaddy", ".png", ".jpg")


class ContactFinder:
    """Cache-backed contact finder. .find(name, city, max_n) -> list of
    {first_name, email} (up to max_n, capped at 3)."""

    def __init__(self, config, http):
        self.http = http
        self.api_key = config.parallel_key
        self.cache_path: Path = config.data_dir / "found_contacts.json"
        self.needs_path: Path = config.data_dir / "contact_lookups_needed.csv"
        self.cache = self._load_cache()

    def _load_cache(self) -> dict:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_cache(self) -> None:
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")

    def find(self, name: str, city: str = "", max_n: int = 3) -> list[dict]:
        max_n = min(max_n, 3)                 # never need more than 3
        key = _norm(name)
        if not key:
            return []
        if key in self.cache:                 # cached (incl. [] = known-none)
            return self.cache[key][:max_n]

        if self.api_key:                      # production: live Parallel lookup
            contacts = self._parallel_find(name, city, max_n)
            self.cache[key] = contacts
            self._save_cache()
            return contacts[:max_n]

        self._record_needed(name, city)       # cache-only: log the miss
        return []

    # ---- live Parallel Search API ----
    def _parallel_find(self, name: str, city: str, max_n: int) -> list[dict]:
        loc = f" in {city}, Bulgaria" if city else " in Bulgaria"
        body = {
            "objective": (f'Find up to 3 contact email addresses for people who work at '
                          f'"{name}"{loc}, and the first name of each person. '
                          f'Prefer owners, managers and HR. Return name and email together.'),
            "search_queries": [
                f"{name} {city} email contact",
                f"{name} управител мениджър email",
                f"{name} contact us екип",
            ],
            "mode": "basic",
            "advanced_settings": {"max_results": 10},
        }
        data = self.http.post_json(PARALLEL_URL, body,
                                   {"x-api-key": self.api_key, "Content-Type": "application/json"})
        if not data:
            return []
        return _extract_contacts(data.get("results", []), max_n)

    def _record_needed(self, name: str, city: str) -> None:
        new = not self.needs_path.exists()
        with open(self.needs_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["company_name", "city",
                            "found (JSON list of {first_name,email}, up to 3)"])
            w.writerow([name, city, ""])


def build_contact_finder(config, http):
    """Return a ContactFinder if enabled, else None."""
    if not config.get("enrich", "find_website", default=True):
        return None
    finder = ContactFinder(config, http)
    mode = "LIVE (Parallel API)" if config.parallel_key else "CACHE-ONLY (fill data/found_contacts.json)"
    log.info("Contact finder: %s", mode)
    return finder


# ---- extraction: pull up to max_n {first_name, email} from search results ----
def _extract_contacts(results: list, max_n: int) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for r in results:
        if len(out) >= max_n:
            break
        text = " ".join(filter(None, [r.get("title", ""),
                                       " ".join(r.get("excerpts", []) or [])]))
        for m in _EMAIL_RE.finditer(text):
            email = m.group(0).lower()
            if email in seen or any(j in email for j in JUNK_EMAIL):
                continue
            seen.add(email)
            out.append({"first_name": _first_name_near(text, m.start(), email), "email": email})
            if len(out) >= max_n:
                break
    return out


def _first_name_near(text: str, pos: int, email: str) -> str:
    """Best-effort: a capitalised name token just before the email, else derive
    from a firstname.lastname local part, else '' (generic)."""
    window = text[max(0, pos - 60):pos]
    names = re.findall(r"[A-ZА-Я][a-zа-я]{2,}", window)
    if names:
        return names[-1]
    local = email.split("@", 1)[0]
    parts = [p for p in re.split(r"[._\-]", local) if p.isalpha() and len(p) > 1]
    if len(parts) >= 2 and parts[0] not in _ROLE:
        return parts[0].capitalize()
    return ""


_ROLE = {"info", "office", "hr", "contact", "sales", "team", "hello", "no",
         "noreply", "reception", "booking", "admin", "support"}


def _norm(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    for suf in (" еоод", " оод", " ад", " еад", " ет", " eood", " ood", " ad", " ltd"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return "".join(ch for ch in s if ch.isalnum())
