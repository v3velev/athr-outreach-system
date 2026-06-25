"""ICP filtering - decide whether a scraped job is a real athr prospect.

Order of checks (cheapest / highest-signal first):
  1. geo        - job must be located in an allowed country (drops "work abroad")
  2. company    - drop competitor/staffing-agency posters (NEVER pitch a recruiter)
  3. title      - drop saturated verticals (IT) by keyword
  4. has-company- need a usable employer name to enrich

Returns (keep: bool, reason: str). reason is "" when kept.

Design note: every rule is config-driven (config.yaml -> icp:*). No hard-coded
judgement calls live in code, so the client can tune targeting without edits.
"""

from __future__ import annotations

from .models import Job


class ICPFilter:
    def __init__(self, config):
        icp = config.get("icp", default={})
        self.allowed_countries = {c.upper() for c in
                                  (icp.get("geo", {}).get("allowed_countries") or ["BG"])}
        self.exclude_title = [k.lower() for k in (icp.get("exclude_title_keywords") or [])]
        self.exclude_company = [k.lower() for k in (icp.get("exclude_company_keywords") or [])]

    def evaluate(self, job: Job) -> tuple[bool, str]:
        # 1. geo
        if job.country and job.country.upper() not in self.allowed_countries:
            return False, f"foreign_location:{job.country}"

        # 2. company / agency filter
        name = (job.company_name or "").lower()
        if not name.strip():
            return False, "no_company_name"
        # reject a "name" that is only a legal-form suffix (e.g. just "ООД")
        bare = name.replace(".", " ")
        for suf in ("еоод", "оод", "ад", "еад", "ет", "eood", "ood", "ad", "ltd"):
            bare = bare.replace(suf, " ")
        if not bare.strip() or len(bare.strip()) < 2:
            return False, "no_real_company_name"
        for kw in self.exclude_company:
            if kw in name:
                return False, f"agency_or_competitor:{kw.strip()}"

        # 3. title keyword exclusion (IT, etc.)
        title = (job.title or "").lower()
        for kw in self.exclude_title:
            if kw in title:
                return False, f"excluded_title:{kw}"

        return True, ""
