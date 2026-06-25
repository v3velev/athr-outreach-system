# athr - Outreach System (Technical Documentation)

How both systems actually work: data sources, APIs, the step-by-step pipeline,
and the decision behind each step. Companion to the plain-language overview.

---

## Architecture

Both systems share ONE enrichment + messaging + sending engine. They differ only
in **how companies enter the pipeline**:

- **System 1 (Active Hiring)** - companies enter from **job boards** (they're
  hiring now → intent signal). Runs daily, automated.
- **System 2 (Full Market)** - companies enter from a **pre-built market list**
  (Google Maps / OpenMart). Runs as a campaign over months.

```
SYSTEM 1: job boards ─┐
                      ├─► ICP filter ─► dedup ─► ENRICH ─► VERIFY ─► WRITE ─► SEND
SYSTEM 2: market list ┘     (shared engine from "ENRICH" onward)
```

---

## The APIs (and why each)

| API | Role | Endpoint / auth | Cost |
|---|---|---|---|
| **zaplata.bg** (scrape) | Source of live job postings | HTML + JSON-LD, `curl_cffi` Chrome impersonation | free |
| **Prospeo** | Find decision-maker + verified email (LinkedIn-backed) | `POST api.prospeo.io/...`, header `X-KEY` | ~1-2 credits/co |
| **Parallel Search** | Find emails+names when not in Prospeo | `POST api.parallel.ai/v1/search`, header `x-api-key` | ~$0.0064/co |
| **Trykitt** | Verify a found email is deliverable | `POST api.trykitt.ai/job/verify_email`, header `x-api-key` | ~$0.0015/email |
| **Anthropic Claude** | Write the personalised BG email | `POST api.anthropic.com/v1/messages`, header `x-api-key` | ~$0.01-0.03/email |
| **SmartLead** | Send + sequence + reply tracking | `POST server.smartlead.ai/api/v1/...`, query `?api_key=` | existing plan |
| **Google Maps / OpenMart** | Build the System-2 market list | (System 2 sourcing) | per tool |

Every API is optional at runtime: if a key is missing, that stage runs in a safe
fallback (mock writer / MX-only verify / cache-only finder), so the whole pipeline
runs end-to-end before any key is entered.

---

## System 1 - Active Hiring, step by step

### Step 1 - Scrape new job postings (zaplata.bg)
- We fetch each ICP **sector page** (`/hoteli/`, `/proizvodstvo/`, `/shofiori-i-kurieri/`, etc.) with `?page=N` pagination, using `curl_cffi` impersonating Chrome (clean TLS fingerprint, no blocking).
- Each posting carries a **schema.org JSON-LD `JobPosting`** block - we parse it for: title, company, city, country, salary, **employer website (when present)**, description, and exact `datePosted`.
- **Decision - read JSON-LD, not HTML:** structured data is stable and exact; we don't scrape fragile page markup.
- **Decision - salary is read as EUR:** zaplata's JSON-LD mislabels the currency as BGN after Bulgaria's euro switch; we correct it.
- **Incremental:** every job ID we've processed is stored in a local **SQLite** database. We scrape newest-first and stop a sector once we hit already-seen IDs, so each run only processes genuinely new postings (and a missed day self-heals).

### Step 2 - ICP filter (drop the bad ones)
Each posting passes cheap, high-signal gates in order:
1. **Geo** - keep only `country == BG` (drops the ~15% "work abroad" agency posts).
2. **Agency/competitor** - drop employers whose name matches a blocklist (консулт, персонал, human, рикрут, подбор, агенц, staffing, recruit, talent, athr itself...). We never pitch recruitment to a recruiter.
3. **Saturated roles** - drop IT/dev titles by keyword.
4. **Junk** - drop a "name" that's only a legal suffix (e.g. just "ООД").

### Step 3 - Dedup
- **One outreach per company**, even if it posted 10 roles (normalised company key, legal-suffix-stripped).
- **Never re-contact** a company already emailed (checked against the SQLite `contacted` table, across all past runs).

### Step 4 - Enrichment (find a contact) - the exact order
> The whole point: get a **verified** email, with a first name when possible.

1. **Prospeo first.** `POST /search-person` filtered by the company **domain**
   (`company.websites`), `person_seniority` C-Suite → Manager, **no department
   filter**, location `"Bulgaria #BG"`, verified-email only. It returns people
   with **masked** emails. We rank by title and call `POST /enrich-person`
   (`{data:{person_id}, only_verified_email:true}`) to **reveal** the real email
   (already BounceBan-verified, and we have the name → personalised).
   - **Decision - domain-only Prospeo:** searching by Cyrillic company *name*
     silently returns a wrong generic person; searching by domain is reliable.
2. **If Prospeo finds nobody → find the email ourselves:**
   - If the posting **has a website** → scrape its homepage/contact pages for emails (free).
   - If **no website** → **Parallel Search** finds **up to 3 `{first_name, email}`** in one call.
3. **Verify each found email with Trykitt** (`validity == valid`). Try up to **3 candidates**: if one isn't valid, try the next; if none verify, **skip the company**. (A free MX/DNS check pre-filters dead domains first, to save Trykitt calls.)
   - **Decision - verify everything we find:** scraped/found emails aren't pre-checked; unverified sends bounce and destroy sender reputation.

### Step 5 - Write the message (Claude)
- A **rule** picks the pitch angle from the job's sector (hotels → camerieras/reception/kitchen, drivers → couriers, etc.).
- **Claude** writes the actual short Bulgarian email around the live facts (company, exact role, city) and athr's voice (licensed, fast, result-based).
- **Name handling:** have a first name → personalised greeting; no name (e.g. `office@`) → identical message minus the name ("Здравейте,").
- Hard rules enforced in code: single hyphens only, no spam words, subject kept short and single-line.

### Step 6 - Send (SmartLead)
- Each lead is added to a SmartLead campaign with the finished email baked into **custom fields** - the campaign body is just `{{subject}}` / `{{message}}`, so every lead carries its own copy.
- Campaign settings: **plain text, open-tracking off, stop-on-reply, domain-level rate limit, bounce auto-pause** at 5%, Europe/Sofia business hours.

### Step 7 - Volume + scheduling
- Two ceilings: **max sends/run** (reputation ramp) and **max attempts/run** (runtime bound - since only ~5-10% of companies are contactable, this stops the run from chasing forever).
- Runs once a day via **cron / launchd / systemd**. Hostable on a $5 VPS or the client's own always-on computer.

---

## System 2 - Full Market, step by step

### Step 1 - Build the market list (one-time, then refreshed)
- Source the **company universe** from **Google Maps / OpenMart** by ICP vertical (hotels, restaurants, manufacturing, logistics, retail, construction...). This is the one source that actually covers Bulgaria's SMB long tail, with name + website + phone.
- **Decision - not Prospeo/LinkedIn for sourcing:** small BG businesses aren't in people-databases; local-business data is the right universe.

### Step 2 - Enrich (same engine as System 1)
- For each company: **Parallel** finds `{first_name, email}` (or scrape the site) → **Trykitt** verifies → keep only deliverable.
- Prospeo is used for the bigger, LinkedIn-covered names.

### Step 3 - Dedup + load
- Dedup the list against prior campaigns **and** the System-1 contacted set (no one gets both).
- Load into a SmartLead campaign with a **multi-email sequence**.

### Step 4 - Lifecycle (how the client operates it)
- The sequence runs over **a couple of months**.
- When it ends, the client chooses in the software: **re-send** to the same list, or **re-scrape/refresh** for new companies.
- **Every 4-6 months**, refresh the list to keep reaching new businesses.
- **Decision - intent vs coverage:** System 1 = warm/timed daily; System 2 = whole market on repeat. Same engine, different entry point.

---

## Deliverability safeguards (why this stays out of spam)
- **Verified emails only** (Prospeo BounceBan / Trykitt SMTP / MX pre-filter).
- **No double-contact** (persistent SQLite memory of every company reached).
- **Controlled pace** + plain-text + no open-tracking + bounce auto-pause.
- **Agency/foreign/junk filtered** before any send.

## Data + state
- **SQLite** (`data/athr.db`): seen job IDs, contacted companies, full per-run audit log. Single local file, no server.
- **Caches:** found contacts (`found_contacts.json`) so we never re-pay for the same lookup.
- **Secrets** in `.env` (Prospeo, Parallel, Trykitt, Anthropic, SmartLead keys) - never committed.
