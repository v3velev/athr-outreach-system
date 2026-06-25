"""The daily pipeline orchestrator.

Flow per run:
  scrape (all sources) -> mark seen -> ICP filter -> company dedup
    -> enrich (decision-maker + email) -> write Bulgarian message
    -> send to SmartLead (or mock CSV) -> record + summarise

Every job is recorded with its final status and drop reason, so nothing is
silently lost and the run summary is honest.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .enrich import build_enricher
from .icp import ICPFilter
from .messaging import build_message_provider
from .models import OutreachItem, RunStats
from .notify import notify
from .sender import build_sender
from .sources import build_sources
from .store import Store, company_key
from .utils import HttpClient, get_logger

log = get_logger()


def run(config: Config, dry_run_send: bool = False, max_new: int | None = None) -> RunStats:
    stats = RunStats(started_at=_now())
    log.info("athr daily outreach starting | mode: %s", config.mode_banner())

    http = HttpClient(delay_range=tuple(config.get("run", "request_delay_seconds",
                                                   default=[1.5, 3.5])))
    store = Store(config.data_dir / "athr.db")
    icp = ICPFilter(config)
    enricher = build_enricher(config, http)
    messenger = build_message_provider(config, http)
    sender = build_sender(config, http)

    # cap = ceiling on OUTREACH (sends) per run, for reputation ramp.
    cap = max_new if max_new is not None else config.get(
        "run", "max_new_companies_per_run", default=40)
    # Independent ceiling on ATTEMPTS (companies scraped+enriched), to bound
    # runtime/cost. Only ~5-10% of companies are contactable (most zaplata posts
    # don't expose a website), so without this the run would chase the cap
    # through hundreds of companies. We stop at whichever ceiling hits first and
    # report the actual yield.
    max_attempts = config.get("run", "max_attempts_per_run", default=200)
    max_contacts = max(1, int(config.get("enrich", "max_contacts_per_company", default=1)))
    run_id = store.start_run()

    scrape_budget = max_attempts
    seen_company_keys: set[str] = set()
    processed_companies = 0   # contactable companies we actually engaged (~= sends)

    def handle_job(job) -> bool:
        """Process one job end-to-end. Returns True if it consumed a company
        slot (i.e. passed dedup), so the caller can count it toward the cap.
        Any exception in here is caught by the caller - one bad job never
        aborts the run."""
        item = OutreachItem(job=job)

        # ---- ICP filter ----
        keep, reason = icp.evaluate(job)
        if not keep:
            item.status, item.drop_reason = "filtered", reason
            stats.drop(reason)
            store.record_item(run_id, item)
            return False
        stats.after_icp += 1

        # ---- company-level dedup ----
        ckey = company_key(job.company_name)
        if ckey in seen_company_keys:
            item.status, item.drop_reason = "skipped", "dup_company_this_run"
            stats.drop("dup_company_this_run")
            store.record_item(run_id, item)
            return False
        if config.get("filters", "skip_already_contacted", default=True) \
                and store.is_contacted(ckey):
            item.status, item.drop_reason = "skipped", "already_contacted"
            stats.drop("already_contacted")
            store.record_item(run_id, item)
            return False
        seen_company_keys.add(ckey)
        stats.after_dedup += 1

        # ---- enrich (up to N decision-makers at this company) ----
        contacts = [c for c in enricher.find_contacts(job, max_n=max_contacts) if c.is_usable]
        if not contacts:
            item.status, item.drop_reason = "skipped", "no_contact_found"
            stats.drop("no_contact_found")
            store.record_item(run_id, item)
            # Do NOT count toward the daily cap: we sent nothing. The cap limits
            # OUTREACH volume (for reputation ramp); total attempts are already
            # bounded by the scrape budget. Otherwise uncontactable companies
            # (the ~60% with no findable email) would silently exhaust the cap.
            return False
        stats.enriched += 1
        company_emailed = False

        for contact in contacts:
            sub = OutreachItem(job=job, contact=contact)
            try:
                message = messenger.generate(job, contact)
            except Exception as exc:
                message = None
                sub.notes = f"message_exception: {exc}"
            if not message or not message.body:
                sub.status, sub.drop_reason = "error", "message_generation_failed"
                stats.drop("message_generation_failed")
                stats.errors += 1
                store.record_item(run_id, sub)
                continue
            sub.message = message
            stats.generated += 1

            if dry_run_send:
                sub.status, sub.notes = "generated", "dry_run_send (not sent)"
                store.record_item(run_id, sub)
                continue

            ok, note = sender.send(job, contact, message)
            sub.sent_ok, sub.notes = ok, note
            if ok:
                sub.status = "sent"
                stats.sent += 1
                company_emailed = True
            else:
                sub.status, sub.drop_reason = "error", note
                stats.errors += 1
            store.record_item(run_id, sub)
            log.info("  [%s] %s <- %s [%s] (%s)", sub.status,
                     (job.company_name or "")[:40], contact.email,
                     contact.job_title, message.generator)

        if company_emailed:
            store.mark_contacted(ckey, job.company_name, contacts[0].email,
                                 config.smartlead_campaign_id)
        return True

    try:
        for source in build_sources(config, http, store):
            if processed_companies >= cap:
                break
            log.info("--- source: %s ---", source.name)
            try:
                for job in source.fetch_new_jobs(scrape_budget):
                    # Stop BEFORE marking seen once the cap is hit, so overflow
                    # jobs are picked up next run rather than silently dropped.
                    if processed_companies >= cap:
                        break
                    stats.scraped += 1
                    store.mark_seen(job.uid, job.source, job.external_id, job.company_name)
                    stats.new_jobs += 1
                    try:
                        if handle_job(job):
                            processed_companies += 1
                    except Exception as exc:   # one bad job must not kill the run
                        log.warning("  job %s failed: %s", job.uid, exc)
                        stats.errors += 1
            except Exception as exc:           # one bad source must not kill the run
                log.error("source %s crashed, skipping: %s", source.name, exc)
                continue

    finally:
        sender.close()
        stats.finished_at = _now()
        store.finish_run(run_id, stats)
        store.close()

    mode = "DRY-RUN" if dry_run_send else config.mode_banner()
    notify(config, stats, mode)
    return stats


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
