#!/usr/bin/env python3
"""Create the SmartLead campaign the daily run feeds into.

Run ONCE. It creates a campaign, installs a 1-step sequence whose subject/body
are just the {{subject}} / {{message}} merge fields (each lead carries its own
fully-written copy), and applies plain-text + no-open-tracking settings that
match the proven deliverability pattern. Prints the campaign id to paste into
.env as SMARTLEAD_CAMPAIGN_ID.

Usage:
    python scripts/create_campaign.py --name "athr - daily jobs (BG)"

Needs SMARTLEAD_API_KEY in .env. Does NOT start the campaign (stays DRAFT) and
does NOT assign mailboxes - do that in the SmartLead UI, then set it to START.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athr.config import load_config
from athr.utils import HttpClient

BASE = "https://server.smartlead.ai/api/v1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="athr - daily jobs (BG)")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.smartlead_key:
        print("ERROR: SMARTLEAD_API_KEY not set in .env"); return 1
    http = HttpClient()
    key = cfg.smartlead_key
    cid = f"&client_id={cfg.smartlead_client_id}" if cfg.smartlead_client_id else ""

    # 1. create campaign
    r = http.post_json(f"{BASE}/campaigns/create?api_key={key}{cid}",
                       {"name": args.name}, {"Content-Type": "application/json"})
    campaign_id = None
    if r:
        campaign_id = r.get("id") or r.get("campaign_id") or (r.get("data") or {}).get("id")
    if not campaign_id:
        print("ERROR creating campaign:", r); return 1
    print(f"Created campaign id = {campaign_id}")

    # 2. install the single-step merge-field sequence
    seq = {"sequences": [{
        "seq_number": 1,
        "seq_delay_details": {"delay_in_days": 0},
        "subject": "{{subject}}",
        "email_body": "{{message}}",
    }]}
    http.post_json(f"{BASE}/campaigns/{campaign_id}/sequences?api_key={key}{cid}",
                   seq, {"Content-Type": "application/json"})

    # 3. deliverability-friendly settings (plain text, no open tracking)
    settings = {
        "send_as_plain_text": True,
        "track_settings": ["DONT_TRACK_EMAIL_OPEN"],
        "stop_lead_settings": "REPLY_TO_AN_EMAIL",
        "add_unsubscribe_tag": False,
        # If you email up to 3 people at one company, pause the rest the moment
        # anyone there replies - avoids talking over a live conversation.
        "auto_pause_domain_leads_on_reply": True,
        # Don't hammer one company's domain with all its contacts at once.
        "domain_level_rate_limit": True,
        # Auto-pause the campaign if bounces spike (protects sender reputation).
        "bounce_autopause_threshold": "5",
    }
    http.post_json(f"{BASE}/campaigns/{campaign_id}/settings?api_key={key}{cid}",
                   settings, {"Content-Type": "application/json"})

    # Sending schedule: Bulgaria business hours, weekdays, conservative volume.
    schedule = {
        "timezone": "Europe/Sofia",
        "days_of_the_week": [1, 2, 3, 4, 5],
        "start_hour": "09:00",
        "end_hour": "18:00",
        "min_time_btw_emails": 10,
        "max_new_leads_per_day": 30,
    }
    http.post_json(f"{BASE}/campaigns/{campaign_id}/schedule?api_key={key}{cid}",
                   schedule, {"Content-Type": "application/json"})

    print("\nDone. Next:")
    print(f"  1. put this in .env ->  SMARTLEAD_CAMPAIGN_ID={campaign_id}")
    print("  2. in the SmartLead UI: assign sending mailboxes + set schedule")
    print("  3. start the campaign when ready (it is DRAFT now)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
