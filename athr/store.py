"""SQLite persistence: the 'memory' that makes the daily run incremental.

Three jobs:
  1. seen_jobs      - every job id we've ever processed (so we only act on NEW ones)
  2. contacted      - every company we've emailed (so we never double-contact)
  3. runs / items   - an audit log of each run and every outreach item

This is what gives us exact "what's new since last run" without a fragile
time filter, and self-healing if a day is skipped.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import OutreachItem, RunStats


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_jobs (
                uid          TEXT PRIMARY KEY,   -- "zaplata:697127"
                source       TEXT,
                external_id  TEXT,
                company_name TEXT,
                first_seen   TEXT
            );
            CREATE TABLE IF NOT EXISTS contacted (
                company_key  TEXT PRIMARY KEY,   -- normalised company name or domain
                company_name TEXT,
                email        TEXT,
                contacted_at TEXT,
                campaign_id  TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                finished_at TEXT,
                stats_json TEXT
            );
            CREATE TABLE IF NOT EXISTS items (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id    INTEGER,
                job_uid   TEXT,
                status    TEXT,
                row_json  TEXT,
                created_at TEXT
            );
            """
        )
        self.conn.commit()

    # ---- seen jobs (incremental scrape watermark) ----
    def is_seen(self, uid: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen_jobs WHERE uid=?", (uid,))
        return cur.fetchone() is not None

    def mark_seen(self, uid: str, source: str, external_id: str, company_name: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_jobs(uid, source, external_id, company_name, first_seen) "
            "VALUES (?,?,?,?,?)",
            (uid, source, external_id, company_name, _now()),
        )
        self.conn.commit()

    def seen_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]

    # ---- contacted companies (dedup across runs) ----
    def is_contacted(self, company_key: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM contacted WHERE company_key=?", (company_key,))
        return cur.fetchone() is not None

    def mark_contacted(self, company_key: str, company_name: str, email: str,
                       campaign_id: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO contacted(company_key, company_name, email, contacted_at, campaign_id) "
            "VALUES (?,?,?,?,?)",
            (company_key, company_name, email, _now(), campaign_id),
        )
        self.conn.commit()

    # ---- run + item audit log ----
    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at) VALUES (?)", (_now(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats: RunStats) -> None:
        from dataclasses import asdict
        self.conn.execute(
            "UPDATE runs SET finished_at=?, stats_json=? WHERE run_id=?",
            (_now(), json.dumps(asdict(stats), ensure_ascii=False), run_id),
        )
        self.conn.commit()

    def record_item(self, run_id: int, item: OutreachItem) -> None:
        self.conn.execute(
            "INSERT INTO items(run_id, job_uid, status, row_json, created_at) VALUES (?,?,?,?,?)",
            (run_id, item.job.uid, item.status,
             json.dumps(item.flat(), ensure_ascii=False, default=str), _now()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def company_key(name: str) -> str:
    """Normalise a company name into a stable dedup key.

    Lowercases, strips common BG legal suffixes and punctuation so
    'ЕВРОХОТЕЛИ АД' and 'Еврохотели АД ' collapse to the same key.
    """
    if not name:
        return ""
    s = name.lower().strip()
    for suffix in (" еоод", " оод", " ад", " еад", " ет", " дззд",
                   " eood", " ood", " ad", " ead", " ltd", " gmbh"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return "".join(ch for ch in s if ch.isalnum())
