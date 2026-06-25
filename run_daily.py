#!/usr/bin/env python3
"""athr daily outreach - main entrypoint.

Typical use:
    python run_daily.py                 # one daily run, current config
    python run_daily.py --dry-run       # do everything EXCEPT actually send
    python run_daily.py --max-new 5     # tiny run for testing
    python run_daily.py --status        # print DB stats and exit

The scheduler (cron / launchd / systemd) just calls `python run_daily.py`.
"""

from __future__ import annotations

import argparse
import sys

from athr.config import load_config
from athr.pipeline import run
from athr.store import Store


def main() -> int:
    ap = argparse.ArgumentParser(description="athr daily job-to-outreach automation")
    ap.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    ap.add_argument("--env", help="path to .env (default: ./.env)")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the full pipeline but do NOT send (writes nothing to SmartLead)")
    ap.add_argument("--max-new", type=int, default=None,
                    help="override max new companies this run (good for testing)")
    ap.add_argument("--status", action="store_true",
                    help="print database stats (seen jobs, contacted companies) and exit")
    args = ap.parse_args()

    config = load_config(args.config, args.env)

    if args.status:
        store = Store(config.data_dir / "athr.db")
        seen = store.seen_count()
        contacted = store.conn.execute("SELECT COUNT(*) FROM contacted").fetchone()[0]
        runs = store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        store.close()
        print(f"seen_jobs={seen}  contacted_companies={contacted}  runs={runs}")
        print(f"mode: {config.mode_banner()}")
        print(f"data dir: {config.data_dir}")
        return 0

    from athr.utils import RunLock, get_logger
    with RunLock(config.data_dir / "run.lock") as ok:
        if not ok:
            get_logger().warning("Another run is already in progress - exiting.")
            return 0
        stats = run(config, dry_run_send=args.dry_run, max_new=args.max_new)

    # non-zero exit if the run produced only errors and no output (helps cron alerting)
    if stats.errors and stats.sent == 0 and stats.generated == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
