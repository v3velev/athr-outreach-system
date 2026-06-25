#!/usr/bin/env python3
"""Offline tests - no network, no API keys. Validates the filtering, dedup,
message-cleaning, and the full mock pipeline against in-memory fixture jobs.

Run either way:
    python tests/test_pipeline.py        # standalone (no pytest needed)
    pytest tests/test_pipeline.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athr.config import load_config
from athr.enrich import MockProvider as MockEnricher
from athr.icp import ICPFilter
from athr.messaging import MockProvider as MockMessenger, clean
from athr.models import Job
from athr import sources as sources_mod
from athr.store import company_key


# ---- fixtures: realistic jobs mirroring what we actually scraped ----
def fixture_jobs():
    return [
        Job(source="zaplata", external_id="900001", url="u1",
            title="Камериер/ка", company_name="ЕВРОХОТЕЛИ АД",
            sector="hoteli", city="София", country="BG", date_posted="2026-06-24"),
        # agency -> must be dropped
        Job(source="zaplata", external_id="900002", url="u2",
            title="Шофьор", company_name="HUMAN POWER BG",
            sector="shofiori-i-kurieri", city="Пловдив", country="BG"),
        # foreign -> must be dropped
        Job(source="zaplata", external_id="900003", url="u3",
            title="Спа терапевт", company_name="СИКАТ ЕООД",
            sector="hoteli", city="", country="IE"),
        # IT -> must be dropped by title keyword
        Job(source="zaplata", external_id="900004", url="u4",
            title="Senior Backend Developer", company_name="Софтуер ЕООД",
            sector="proizvodstvo", country="BG"),
        # duplicate company of #1 (different legal-form spacing) -> dedup
        Job(source="zaplata", external_id="900005", url="u5",
            title="Рецепционист", company_name="Еврохотели АД",
            sector="hoteli", city="София", country="BG", date_posted="2026-06-24"),
    ]


class FakeSource:
    name = "fake"

    def __init__(self, config, http, store):
        self.store = store

    def fetch_new_jobs(self, max_new):
        for j in fixture_jobs():
            if not self.store.is_seen(j.uid):
                yield j


def test_icp_filter():
    cfg = load_config()
    icp = ICPFilter(cfg)
    jobs = {j.external_id: j for j in fixture_jobs()}
    assert icp.evaluate(jobs["900001"])[0] is True
    assert icp.evaluate(jobs["900002"]) == (False, "agency_or_competitor:human")
    assert icp.evaluate(jobs["900003"])[0] is False         # foreign
    assert icp.evaluate(jobs["900004"])[0] is False         # IT
    print("  ok: ICP filter (good/agency/foreign/IT)")


def test_company_key_dedup():
    assert company_key("ЕВРОХОТЕЛИ АД") == company_key("Еврохотели АД ")
    assert company_key("Тейсти Лаб ЕООД") != company_key("ЕВРОХОТЕЛИ АД")
    print("  ok: company_key dedup normalisation")


def test_clean_removes_dashes():
    assert "—" not in clean("a — b")
    assert "--" not in clean("a -- b")
    assert clean("a — b") == "a - b"
    print("  ok: clean() strips em/en/double dashes")


def test_mock_enrich_and_message():
    cfg = load_config()
    job = fixture_jobs()[0]
    contacts = MockEnricher().find_contacts(job, max_n=3)
    assert len(contacts) == 3 and len({c.email for c in contacts}) == 3
    contact = contacts[0]
    assert contact.is_usable and "@" in contact.email
    msg = MockMessenger(cfg).generate(job, contact)
    assert msg and msg.body and job.company_name in msg.body
    assert "—" not in msg.body and "--" not in msg.body
    print("  ok: mock enrich + BG message generation")


def test_full_pipeline_mock(monkeypatch=None):
    cfg = load_config()
    # point data dir at a temp folder so we don't touch real state
    tmp = tempfile.mkdtemp()
    cfg.raw.setdefault("run", {})["data_dir"] = tmp
    # force pure MOCK mode regardless of any keys in .env: no website scrape,
    # no live API keys -> Mock enricher / Mock messenger / Mock sender.
    cfg.raw.setdefault("enrich", {})["website_scrape"] = False
    cfg.prospeo_key = cfg.anthropic_key = ""
    cfg.smartlead_key = cfg.smartlead_campaign_id = ""
    # inject the fake source
    orig = sources_mod.build_sources
    sources_mod.build_sources = lambda c, h, s: [FakeSource(c, h, s)]
    try:
        from athr.pipeline import run
        import athr.pipeline as pl
        pl.build_sources = sources_mod.build_sources
        stats = run(cfg, dry_run_send=False, max_new=10)
    finally:
        sources_mod.build_sources = orig
    # 5 fixtures: 1 good kept, 1 agency, 1 foreign, 1 IT dropped, 1 dup company
    assert stats.scraped == 5
    assert stats.after_icp == 2          # the good one + its duplicate pass ICP
    assert stats.after_dedup == 1        # duplicate collapses to one company
    assert stats.sent == 1               # one message produced + mock-sent
    assert stats.drop_reasons.get("dup_company_this_run") == 1
    print("  ok: full mock pipeline (scrape->filter->dedup->enrich->msg->send)")


def test_hardening_edge_cases():
    """Regression tests for the adversarial cases found during QA."""
    from athr.messaging import _short_role, _oneline
    from athr.enrich import _name_from_email

    # subjects/roles must never carry newlines/tabs or be blank
    assert _short_role(" ") == "позиция"
    assert "\n" not in _short_role("Камериер\n\nспешно")
    assert "\t" not in _short_role("Готвач\tсутрин")
    assert _oneline("a\n\nb\tc   d") == "a b c d"
    assert _oneline("x" * 200, 50).endswith("…") and len(_oneline("x" * 200, 50)) == 50

    # email->name must reject role/junk locals and single tokens
    assert _name_from_email("no-reply@x.com") == ("", "")
    assert _name_from_email("hello@x.com") == ("", "")
    assert _name_from_email("hellotastylab@gmail.com") == ("", "")
    assert _name_from_email("office@x.bg") == ("", "")
    assert _name_from_email("georgi.petrov@x.bg") == ("Georgi", "Petrov")
    print("  ok: hardening edge cases (subjects, names)")


def test_pipeline_survives_crashing_source():
    """A source that raises mid-stream must not abort the whole run."""
    cfg = load_config()
    tmp = tempfile.mkdtemp()
    cfg.raw.setdefault("run", {})["data_dir"] = tmp
    cfg.raw.setdefault("enrich", {})["website_scrape"] = False
    cfg.prospeo_key = cfg.anthropic_key = ""
    cfg.smartlead_key = cfg.smartlead_campaign_id = ""

    class BoomSource:
        name = "boom"
        def __init__(self, c, h, s): self.store = s
        def fetch_new_jobs(self, max_new):
            yield fixture_jobs()[0]              # one good job
            raise RuntimeError("scraper exploded")

    orig = sources_mod.build_sources
    sources_mod.build_sources = lambda c, h, s: [BoomSource(c, h, s)]
    import athr.pipeline as pl
    pl.build_sources = sources_mod.build_sources
    try:
        stats = pl.run(cfg, dry_run_send=False, max_new=10)  # must NOT raise
    finally:
        sources_mod.build_sources = orig
    assert stats.sent == 1, "the one good job should still have been processed"
    print("  ok: pipeline survives a crashing source")


def main():
    print("running offline tests...")
    test_icp_filter()
    test_company_key_dedup()
    test_clean_removes_dashes()
    test_mock_enrich_and_message()
    test_full_pipeline_mock()
    test_hardening_edge_cases()
    test_pipeline_survives_crashing_source()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
