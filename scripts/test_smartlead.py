#!/usr/bin/env python3
"""Read-only SmartLead connectivity check.

Confirms your SMARTLEAD_API_KEY works and lists your campaigns (so you can find
the campaign id). Makes NO changes. Run after putting the key in .env.

    python scripts/test_smartlead.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athr.config import load_config
from athr.utils import HttpClient

BASE = "https://server.smartlead.ai/api/v1"


def main() -> int:
    cfg = load_config()
    if not cfg.smartlead_key:
        print("SMARTLEAD_API_KEY not set in .env"); return 1
    http = HttpClient()
    cid = f"&client_id={cfg.smartlead_client_id}" if cfg.smartlead_client_id else ""
    # /campaigns/{id} needs an id; use the analytics campaign list for a key check
    url = f"{BASE}/campaigns?api_key={cfg.smartlead_key}{cid}"
    try:
        resp = http.session.get(url, timeout=20)
    except Exception as exc:
        print("Request failed:", exc); return 1
    if resp.status_code != 200:
        print(f"AUTH/REQUEST FAILED: HTTP {resp.status_code}: {resp.text[:200]}")
        return 1
    data = resp.json()
    campaigns = data if isinstance(data, list) else data.get("data", [])
    print(f"OK - key valid. {len(campaigns)} campaign(s) visible:")
    for c in campaigns[:25]:
        print(f"  id={c.get('id')}  status={c.get('status')}  name={c.get('name')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
