"""Enrichment: given a hiring company, find a decision-maker + verified email.

Provider interface with two implementations:
  * ProspeoProvider - real Prospeo Search Person API (POST /search-person, X-KEY).
  * MockProvider    - deterministic fake contacts so the pipeline runs with no key.

The factory picks Prospeo when PROSPEO_API_KEY is set, else Mock. Swapping in a
different enrichment vendor later = add one class + one line in build_enricher.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from .models import Contact, Job
from .utils import get_logger, domain_accepts_mail

log = get_logger()

PROSPEO_SEARCH_URL = "https://api.prospeo.io/search-person"
PROSPEO_ENRICH_URL = "https://api.prospeo.io/enrich-person"
TRYKITT_VERIFY_URL = "https://api.trykitt.ai/job/verify_email"


# --------------------------------------------------------------------------
# Email verification - used on emails we FIND ourselves (website/Parallel),
# since those are not pre-verified like Prospeo's BounceBan-checked emails.
class EmailVerifier:
    name = "base"

    def verify(self, email: str) -> str:
        """Return 'valid' | 'risky' | 'invalid' | 'unknown' | 'error'."""
        return "unknown"

    def is_valid(self, email: str) -> bool:
        return self.verify(email) == "valid"


class TrykittVerifier(EmailVerifier):
    """Real-time SMTP/catch-all verification via Trykitt (proven in prod).
    POST https://api.trykitt.ai/job/verify_email, header x-api-key,
    body {email, treatAliasesAsValid, realtime}. ~$0.0015/email.
    """
    name = "trykitt"

    def __init__(self, config, http):
        self.http = http
        self.api_key = config.trykitt_key

    def verify(self, email: str) -> str:
        data = self.http.post_json(
            TRYKITT_VERIFY_URL,
            {"email": email, "treatAliasesAsValid": True, "realtime": True},
            {"x-api-key": self.api_key, "Content-Type": "application/json"})
        if not data:
            return "error"
        return (data.get("validity") or data.get("displayText") or "unknown").lower()


class MXVerifier(EmailVerifier):
    """Free fallback when no Trykitt key: passes anything on a mail-accepting
    domain. Weaker than Trykitt (can't catch dead mailboxes on live domains)."""
    name = "mx"

    def verify(self, email: str) -> str:
        return "valid" if domain_accepts_mail(email.split("@", 1)[-1]) else "invalid"


def build_verifier(config, http) -> EmailVerifier:
    if config.trykitt_key:
        log.info("Email verify: LIVE (Trykitt real-time SMTP)")
        return TrykittVerifier(config, http)
    log.info("Email verify: MX/DNS only (no TRYKITT_API_KEY) - weaker")
    return MXVerifier()


class EnrichmentProvider:
    live = False

    def find_contacts(self, job: Job, max_n: int = 1) -> list[Contact]:
        """Return up to `max_n` usable decision-makers, best first."""
        raise NotImplementedError


# --------------------------------------------------------------------------
class ProspeoProvider(EnrichmentProvider):
    """Real Prospeo API - TWO steps (verified against the live API 2026-06-24).

    1. search-person  -> finds decision-makers at the company. Emails come back
       MASKED (e.g. a****@x.com, revealed=false). 1 credit per page of 25.
       person.email is an OBJECT: {status, revealed, email, verification_method}.
    2. enrich-person  -> reveals the real email for a chosen person. Body must
       wrap identifiers in `data`: {data:{person_id}, only_verified_email:true}.
       1 credit per revealed match (no charge if no match; no re-charge <90 days).

    The old email-finder/domain-search endpoints are DEPRECATED.
    """
    live = True

    def __init__(self, config, http):
        self.http = http
        self.api_key = config.prospeo_key
        enr = config.get("enrich", default={})
        self.target_titles = enr.get("target_titles") or ["Управител", "CEO", "HR Manager"]
        self.require_verified = enr.get("require_verified_email", True)
        country = enr.get("person_country", "Bulgaria")
        # Prospeo country format is "Name #CC"; map a few, default Bulgaria.
        cc = {"Bulgaria": "BG", "United Kingdom": "GB", "United States": "US"}.get(country, "BG")
        self.location = f"{country} #{cc}"
        pq = enr.get("prospeo") or {}
        self.seniorities = pq.get("seniorities") or ["C-Suite", "Director", "Manager"]
        self.exclude_departments = pq.get("exclude_departments") or []

    @property
    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "X-KEY": self.api_key}

    def find_contacts(self, job: Job, max_n: int = 1) -> list[Contact]:
        candidates = self._search(job)
        contacts: list[Contact] = []
        seen: set[str] = set()
        for person in candidates:
            if len(contacts) >= max_n:
                break
            email = self._reveal_email(person)
            if not email or email in seen:
                continue
            seen.add(email)
            contacts.append(Contact(
                first_name=person.get("first_name", ""),
                last_name=person.get("last_name", ""),
                full_name=person.get("full_name") or
                          f"{person.get('first_name','')} {person.get('last_name','')}".strip(),
                job_title=person.get("current_job_title", ""),
                email=email,
                email_status="VERIFIED",
                linkedin_url=person.get("linkedin_url", ""),
                phone="",
                source="prospeo",
            ))
        return contacts

    def _search(self, job: Job) -> list[dict]:
        """Step 1: PREVIEW everyone relevant at the company (masked emails).

        Broad-by-design (per the agreed strategy): filter by decision-maker
        SENIORITY and EXCLUDE departments we don't pitch, rather than guessing
        exact titles. One masked page = 1 credit; we reveal only the chosen N.

        IMPORTANT (learned from live testing): Prospeo's company-NAME filter does
        not match Bulgarian Cyrillic legal names ('ЕВРОХОТЕЛИ АД') - it silently
        returns a generic wrong person. So we search ONLY by verified domain
        (company.websites), which matches the real company or returns nothing.
        No domain -> no Prospeo lookup.
        """
        domain = _domain_from(job.company_website)
        if not domain:
            return []
        filters: dict = {
            "person_seniority": {"include": self.seniorities},
            "person_location_search": {"include": [self.location]},
            "company": {"websites": {"include": [domain]}},
        }
        if self.exclude_departments:
            filters["person_department"] = {"exclude": self.exclude_departments}
        if self.require_verified:
            filters["person_contact_details"] = {"email": ["VERIFIED"]}

        data = self.http.post_json(PROSPEO_SEARCH_URL,
                                   {"page": 1, "filters": filters},
                                   self._headers, quiet_4xx=True)
        if not data or data.get("error") or not data.get("results"):
            return []
        # validate company domain matches, then rank previews by title relevance
        people = []
        for r in data["results"]:
            p = r.get("person")
            if not p:
                continue
            comp = r.get("company") or {}
            comp_dom = (comp.get("domain") or comp.get("website") or "").lower()
            if domain in comp_dom or not comp_dom:
                people.append(p)
        return sorted(people, key=lambda p:
                      _title_rank(p.get("current_job_title", ""), self.target_titles))

    def _reveal_email(self, person: dict) -> str:
        """Step 2: reveal the real verified email for a person (1 credit)."""
        em = person.get("email") or {}
        if self.require_verified and (em.get("status") or "").upper() != "VERIFIED":
            return ""
        pid = person.get("person_id")
        if not pid:
            return ""
        data = self.http.post_json(
            PROSPEO_ENRICH_URL,
            {"data": {"person_id": pid}, "only_verified_email": True},
            self._headers)
        if not data or data.get("error"):
            return ""
        revealed = (data.get("person") or {}).get("email") or {}
        addr = (revealed.get("email") or "").lower()
        # only accept a fully revealed address (not the masked a****@x.com form)
        if revealed.get("revealed") and "*" not in addr and "@" in addr:
            return addr
        return ""


# --------------------------------------------------------------------------
class MockProvider(EnrichmentProvider):
    """Deterministic fake decision-maker so the pipeline runs end-to-end keyless.

    Generates a stable name + email from the company name. Email status is
    flagged MOCK so it's obvious in outputs and never confused with real data.
    """
    live = False

    # (first, gender) so the mock surname can agree - cosmetic only
    FIRST = ["Георги", "Иван", "Петър", "Николай", "Стефан", "Мария", "Елена", "Десислава"]
    LAST_M = ["Иванов", "Петров", "Димитров", "Колев", "Тодоров"]
    LAST_F = ["Иванова", "Петрова", "Георгиева", "Стоянова", "Тодорова"]
    TITLES = ["Управител", "Мениджър Човешки ресурси", "Оперативен мениджър"]

    def find_contacts(self, job: Job, max_n: int = 1) -> list[Contact]:
        if not job.company_name:
            return []
        h = int(hashlib.md5(job.company_name.encode("utf-8")).hexdigest(), 16)
        domain = _domain_from(job.company_website) or _fake_domain(job.company_name)
        out: list[Contact] = []
        for i in range(max_n):
            first = self.FIRST[(h + i * 3) % len(self.FIRST)]
            female = first in ("Мария", "Елена", "Десислава")
            last = (self.LAST_F if female else self.LAST_M)[(h // 7 + i) %
                    len(self.LAST_F if female else self.LAST_M)]
            email = f"{_translit(first)}.{_translit(last)}@{domain}".lower()
            out.append(Contact(
                first_name=first, last_name=last, full_name=f"{first} {last}",
                job_title=self.TITLES[i % len(self.TITLES)],
                email=email, email_status="MOCK", source="mock",
            ))
        return out


# --------------------------------------------------------------------------
class WebsiteEnrichProvider(EnrichmentProvider):
    """Scrape the company's OWN website for a contact email.

    This is the realistic primary for athr's ICP: small Bulgarian businesses
    (hotels, restaurants, EOODs) that post on zaplata are NOT in people-databases
    like Prospeo, but ~40% expose a website whose homepage/contact page lists a
    real email (hr@, office@, info@, or a personal address). For a recruiter's
    pitch, hr@/jobs@/office@ is exactly the right inbox.

    No API key, no cost - just a few polite page fetches per company.
    """
    live = True

    # contact subpages to try after the homepage (Bulgarian + English)
    SUBPAGES = ["contacts", "kontakti", "contact", "kontakt", "za-kontakti",
                "about", "za-nas"]
    # ranking: hiring inboxes first, then general, then personal/other
    PREFIX_RANK = ["hr", "kariera", "career", "careers", "jobs", "rabota", "job",
                   "office", "ofis", "info", "contact", "kontakt", "reception",
                   "reservations", "rezervacii", "sales", "team"]
    JUNK = ("noreply", "no-reply", "donotreply", "postmaster", "abuse",
            "sentry", "example", "@2x", ".png", ".jpg", ".webp", ".gif",
            "wixpress", "godaddy", "domain", "hostmaster")

    def __init__(self, config, http, finder=None, verifier=None):
        self.http = http
        self.verify_mx = config.get("enrich", "verify_mx", default=True)
        self.verify_attempts = max(1, int(config.get("enrich", "verify_attempts", default=3)))
        self.finder = finder       # WebsiteFinder, used when a job has no website
        self.verifier = verifier   # EmailVerifier (Trykitt) for found emails

    def find_contacts(self, job: Job, max_n: int = 1) -> list[Contact]:
        # Build candidate (first_name, email) pairs from the best free source:
        #   - if the posting HAS a website -> scrape it (free).
        #   - else -> ask the contact finder (Parallel) for up to 3 name+email.
        candidates = self._gather_candidates(job, max_n)
        if not candidates:
            return []

        # Verify candidates: found emails aren't pre-verified, so we VERIFY each
        # (Trykitt) and keep only deliverable ones. Up to `verify_attempts` (3)
        # verification calls per company - if a candidate isn't valid we try the
        # next one; if none verify, we return nothing and the company is skipped.
        out: list[Contact] = []
        attempts = 0
        for first, email in candidates:
            if len(out) >= max_n or attempts >= self.verify_attempts:
                break
            if self.verify_mx and not domain_accepts_mail(email.split("@", 1)[-1]):
                continue   # free pre-filter, doesn't count as an attempt
            attempts += 1
            if self.verifier:
                if self.verifier.verify(email) != "valid":
                    continue   # not verified -> try the next candidate
            out.append(Contact(first_name=first, last_name="",
                               full_name=first, job_title="", email=email,
                               email_status="VERIFIED" if self.verifier else "FOUND",
                               source="found"))
        return out

    def _gather_candidates(self, job: Job, max_n: int) -> list[tuple[str, str]]:
        """Return ranked (first_name, email) candidates, scrape-first then finder."""
        domain = _domain_from(job.company_website)
        if domain:
            emails = self._scrape_emails(domain, max_n)
            return [(_name_from_email(e)[0], e) for e in emails]
        if self.finder:
            found = self.finder.find(job.company_name, job.city, max_n=3)
            return [(c.get("first_name", ""), c["email"]) for c in found if c.get("email")]
        return []

    def _scrape_emails(self, domain: str, max_n: int) -> list[str]:
        base = f"https://{domain}"
        emails: list[str] = []
        seen: set[str] = set()
        for i, path in enumerate([""] + self.SUBPAGES):
            if len(emails) >= max_n + 1 or i > 3:
                break
            if i > 0 and emails:                    # homepage already produced emails
                break
            html = self.http.get(f"{base}/{path}".rstrip("/"), quick=True)
            if not html:
                continue
            for e in _extract_emails(html, domain):
                if e not in seen:
                    seen.add(e)
                    emails.append(e)
        emails.sort(key=lambda e: self._rank(e))
        return emails

    def _rank(self, email: str) -> int:
        local = email.split("@", 1)[0].lower()
        for i, pref in enumerate(self.PREFIX_RANK):
            if local == pref or local.startswith(pref):
                return i
        return len(self.PREFIX_RANK) + 1


class CascadeProvider(EnrichmentProvider):
    """Try several enrichers in order; return the first that yields contacts."""
    live = True

    def __init__(self, providers: list[EnrichmentProvider]):
        self.providers = providers

    def find_contacts(self, job: Job, max_n: int = 1) -> list[Contact]:
        for prov in self.providers:
            try:
                contacts = prov.find_contacts(job, max_n=max_n)
            except Exception as exc:
                log.warning("enricher %s failed: %s", type(prov).__name__, exc)
                contacts = []
            if contacts:
                return contacts
        return []


def build_enricher(config, http) -> EnrichmentProvider:
    """Cascade: website-scrape (free, fits the ICP) -> Prospeo (covers bigger
    companies). Falls back to Mock only when nothing real is available."""
    providers: list[EnrichmentProvider] = []
    # Order per the agreed flow: PROSPEO FIRST (verified, named contacts). Only
    # if Prospeo finds nobody do we find+scrape+verify an email ourselves.
    if config.enrich_live:   # PROSPEO_API_KEY present
        providers.append(ProspeoProvider(config, http))
    if config.get("enrich", "website_scrape", default=True):
        from .website_finder import build_contact_finder
        finder = build_contact_finder(config, http)
        verifier = build_verifier(config, http)
        providers.append(WebsiteEnrichProvider(config, http, finder=finder, verifier=verifier))
    if providers:
        names = " -> ".join(type(p).__name__.replace("Provider", "") for p in providers)
        log.info("Enrichment: LIVE cascade [%s]", names)
        return CascadeProvider(providers)
    log.info("Enrichment: MOCK (no enrichment configured) - placeholder contacts")
    return MockProvider()


# ---- email extraction helpers ----
import re as _re  # noqa: E402  (local alias to keep helpers self-contained)
_EMAIL_RE = _re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ROLE_LOCALS = {"hr", "kariera", "career", "careers", "jobs", "rabota", "office",
                "ofis", "info", "contact", "kontakt", "reception", "sales",
                "team", "reservations", "rezervacii", "support", "help", "admin",
                # never treat these as a person's name
                "no", "noreply", "reply", "donotreply", "do", "not", "mail",
                "email", "hello", "marketing", "booking", "order", "shop"}


def _extract_emails(html: str, domain: str) -> list[str]:
    # guard against pathologically large pages: only scan the first ~2MB
    if len(html) > 2_000_000:
        html = html[:2_000_000]
    found = set(m.group(0).lower() for m in _EMAIL_RE.finditer(html))
    good = []
    for e in found:
        if any(j in e for j in WebsiteEnrichProvider.JUNK):
            continue
        # prefer same-domain or common free providers; drop obvious 3rd-party
        good.append(e)
    # prefer same-domain addresses first
    same = [e for e in good if domain.split(".")[0] in e]
    return same + [e for e in good if e not in same]


def _name_from_email(email: str) -> tuple[str, str]:
    """Derive a first/last name from a personal email ONLY when it's a clear
    firstname.lastname pattern. A single-token local part (info, office,
    hellotastylab, a brand) is NOT a name - we return blank so the greeting
    safely falls back to 'Здравейте,' (avoids 'Hi Hellotastylab')."""
    local = email.split("@", 1)[0].lower()
    if local in _ROLE_LOCALS:
        return "", ""
    parts = [p for p in _re.split(r"[._\-]", local) if p.isalpha() and len(p) > 1]
    # require at least TWO name-like tokens (first + last) to trust it as a person
    if len(parts) < 2:
        return "", ""
    if parts[0] in _ROLE_LOCALS or parts[1] in _ROLE_LOCALS:
        return "", ""
    return parts[0].capitalize(), parts[1].capitalize()


# ---- helpers ----
def _domain_from(website: str) -> str:
    if not website:
        return ""
    if "://" not in website:
        website = "http://" + website
    host = urlparse(website).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _fake_domain(company: str) -> str:
    slug = _translit(re.sub(r"[^\w]", "", company.split()[0] if company.split() else company))
    return f"{slug or 'company'}.bg"


def _title_rank(title: str, targets: list[str]) -> int:
    t = (title or "").lower()
    for i, tgt in enumerate(targets):
        if tgt.lower() in t:
            return i
    return len(targets) + 1


_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
    "ь": "y", "ю": "yu", "я": "ya",
}


def _translit(text: str) -> str:
    out = []
    for ch in text.lower():
        out.append(_CYR.get(ch, ch if ch.isalnum() else ""))
    return "".join(out)
