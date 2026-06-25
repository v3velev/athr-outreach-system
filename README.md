# athr - daily job-to-outreach automation

Every day this system:

1. **Scrapes** new job postings from Bulgarian job boards (zaplata.bg primary).
2. **Filters** them to athr's ICP - drops other recruitment agencies, jobs
   located abroad, IT/saturated roles, and companies already contacted.
3. **Dedupes** to one outreach per company.
4. **Enriches** each hiring company with a decision-maker + verified email (Prospeo).
5. **Writes** a short personalised Bulgarian email (Claude AI, hybrid rules+AI).
6. **Sends** it by adding the lead to a SmartLead campaign.
7. **Reports** a summary so you can see it working.

It is incremental (only acts on genuinely new postings), self-healing (a missed
day is caught up on the next run), and safe (one email per company, never a
competitor).

```
 zaplata.bg ─┐
 jobs.bg* ───┼─► SCRAPE ─► ICP FILTER ─► DEDUPE ─► ENRICH ─► WRITE ─► SEND ─► summary
 jobtiger* ──┘          (agency/foreign   (1 per   (Prospeo)  (Claude)  (Smart
                         /IT/IT removed)   company)                      Lead)
 * optional sources, off by default
```

---

## The key idea: it runs WITHOUT any API keys first

Every external service has a built-in **mock**. With no keys at all you can run
the entire pipeline end-to-end: it really scrapes live jobs, really filters and
dedupes them, generates placeholder contacts + real Bulgarian messages, and
writes them to `data/outbox.csv` for you to inspect. Nothing is sent.

When you add a real key to `.env`, that one stage flips from mock to live.
Nothing else changes. So the hand-off is: **install → run → review the CSV →
add keys when happy → schedule.**

| Stage | Mock (no key) | Live (key in `.env`) |
|-------|---------------|----------------------|
| Enrich | fake but realistic contact | **Prospeo** finds the real decision-maker |
| Message | rule-based BG template | **Claude** writes it (rules pick the angle) |
| Send | appended to `data/outbox.csv` | added to a **SmartLead** campaign |

Scraping is always real (the job boards need no key).

---

## Quick start

```bash
cd athr_outreach_system
bash scripts/install.sh          # venv + dependencies + .env from template

source .venv/bin/activate
python run_daily.py --max-new 3 --dry-run   # tiny test run, nothing sent
python tests/test_pipeline.py               # offline self-tests
```

Then look at `data/outbox.csv` to see exactly what it would send.

---

## Configuration

Two files. **`config.yaml`** = behaviour (safe to commit). **`.env`** = secrets.

### `.env` (copy from `.env.example`)
```
PROSPEO_API_KEY=          # leave blank = mock contacts
ANTHROPIC_API_KEY=        # leave blank = template messages
SMARTLEAD_API_KEY=        # leave blank = write to CSV
SMARTLEAD_CAMPAIGN_ID=    # the campaign new leads go into
```

### `config.yaml` - the things you'll actually tune
- `run.max_new_companies_per_run` - how many companies per day (budget control).
- `sources.zaplata.sectors` - which sectors to crawl (we scrape all, then filter).
- `icp.exclude_company_keywords` - the agency/competitor blocklist.
- `icp.exclude_title_keywords` - saturated roles to skip (IT).
- `enrich.target_titles` - which decision-makers to look for.
- `messaging.*` - sender identity, voice rules, model, subjects.
- `sector_angles` - the pitch angle per sector (the "rules" half of personalization).

---

## Getting the API keys (when ready to go live)

| Service | What it does | Where | Rough cost |
|---------|--------------|-------|-----------|
| **Prospeo** | decision-maker + verified email | prospeo.io → API | ~1 credit / company |
| **Anthropic** | writes the Bulgarian email | console.anthropic.com | ~$0.01-0.03 / email (Sonnet) |
| **SmartLead** | sends / sequences the email | smartlead.ai (you have this) | your existing plan |

To set up the SmartLead campaign once:
```bash
python scripts/test_smartlead.py             # confirm the key works
python scripts/create_campaign.py --name "athr - daily jobs (BG)"
# paste the printed id into .env as SMARTLEAD_CAMPAIGN_ID, then assign
# mailboxes + start the campaign in the SmartLead UI.
```

---

## Hosting - pick one

### Option A - the client's always-on computer (free)
A Mac or PC that stays on. Residential IP, so jobs.bg also works if enabled.
```bash
bash scripts/setup_cron.sh        # macOS: launchd | Linux: cron, daily 09:00
```

### Option B - a $5/mo VPS (cleanest hand-off, recommended)
Always on, nothing on anyone's desk. zaplata.bg works fine from a VPS (no
Cloudflare). See **`deploy/systemd/README.md`** for the full systemd setup.

Either way the scheduler just runs `python run_daily.py` once a day.

---

## Operating it

```bash
python run_daily.py --status      # seen jobs, contacted companies, mode
python run_daily.py               # run now
tail -f data/cron.log             # what the scheduled runs did
```

- `data/outbox.csv` - every lead+message (mock mode) or a mirror of what was sent.
- `data/athr.db` - the memory (seen jobs, contacted companies, full audit log).
- `data/run_summaries/` - one summary file per run.

### Safety built in
- **One email per company**, ever (tracked in the DB across runs).
- **Never a recruitment agency** (competitor keyword filter).
- **Never a job located abroad** (country filter).
- **Never a company already contacted** in a previous run.
- Conservative daily cap so a config mistake can't blast thousands.

---

## Going-live checklist
1. `python run_daily.py --dry-run` and review `data/outbox.csv` - copy reads well?
2. Tune `config.yaml` (sectors, blocklist, cap, sender identity).
3. Add `PROSPEO_API_KEY`, then run `--max-new 3 --dry-run` - real contacts appear?
4. Add `ANTHROPIC_API_KEY` - messages now AI-written.
5. `create_campaign.py`, set `SMARTLEAD_CAMPAIGN_ID`, assign mailboxes.
6. Raise `max_new_companies_per_run` to your target volume.
7. `bash scripts/setup_cron.sh` (or systemd timer on a VPS).

---

## Troubleshooting
- **jobs.bg returns 403 / blocked**: it's behind Cloudflare. It works from a
  residential IP (home computer); on a VPS it's often blocked - leave
  `sources.jobsbg.enabled: false` and rely on zaplata (the backbone).
- **"GAVE UP on …"**: a board soft-rate-limited the IP. Increase
  `run.request_delay_seconds` and/or lower `max_pages_per_*`.
- **No contacts found**: Prospeo had no match for that company. Normal for some
  small firms; they're logged as `no_contact_found` and skipped.
- **Salary looks doubled/halved**: zaplata's structured data mislabels EUR as
  BGN; the scraper already corrects this to EUR.

---

## Project layout
```
athr_outreach_system/
├── run_daily.py            # entrypoint (the scheduler calls this)
├── config.yaml             # behaviour config (targeting, copy, limits)
├── .env.example            # secrets template
├── athr/
│   ├── config.py           # config + mock/live mode detection
│   ├── models.py           # Job / Contact / Message / stats
│   ├── store.py            # SQLite: seen jobs, contacted, audit log
│   ├── utils.py            # Chrome-impersonating HTTP client + retries
│   ├── sources/            # zaplata (primary), jobsbg, jobtiger
│   ├── icp.py              # ICP / agency / geo / IT filtering
│   ├── enrich.py           # Prospeo + mock decision-maker finder
│   ├── messaging.py        # Anthropic + mock BG message writer
│   ├── sender.py           # SmartLead + mock CSV sender
│   ├── pipeline.py         # the orchestrator
│   └── notify.py           # run summaries
├── scripts/                # install, run_once, setup_cron, create_campaign, test_smartlead
├── deploy/                 # launchd plist, systemd unit+timer, crontab
└── tests/                  # offline self-tests
```
