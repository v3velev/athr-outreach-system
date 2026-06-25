"""End-of-run summary - so the client can see at a glance that it's working.

Channels (config.notify.channels): console (always), file (data/run_summaries/),
email (optional SMTP, independent of SmartLead).
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from .models import RunStats
from .utils import get_logger

log = get_logger()


def render_summary(stats: RunStats, mode: str) -> str:
    lines = [
        "athr daily outreach - run summary",
        "=" * 40,
        f"mode            : {mode}",
        f"started         : {stats.started_at}",
        f"finished        : {stats.finished_at}",
        "-" * 40,
        f"jobs scraped    : {stats.scraped}",
        f"new (unseen)    : {stats.new_jobs}",
        f"passed ICP      : {stats.after_icp}",
        f"after dedup     : {stats.after_dedup}",
        f"enriched        : {stats.enriched}",
        f"messages written: {stats.generated}",
        f"SENT/queued     : {stats.sent}",
        f"errors          : {stats.errors}",
    ]
    if stats.after_icp:
        rate = stats.enriched / stats.after_icp * 100
        lines.append(f"contactable rate: {rate:.0f}%  ({stats.enriched}/{stats.after_icp} in-ICP companies had a reachable email)")
    if stats.drop_reasons:
        lines.append("-" * 40)
        lines.append("drop reasons:")
        for reason, count in sorted(stats.drop_reasons.items(), key=lambda x: -x[1]):
            lines.append(f"  {count:>4}  {reason}")
    return "\n".join(lines)


def notify(config, stats: RunStats, mode: str) -> None:
    text = render_summary(stats, mode)
    channels = config.get("notify", "channels", default=["console"])

    if "console" in channels:
        print("\n" + text + "\n")

    if "file" in channels:
        d: Path = config.data_dir / "run_summaries"
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        (d / f"summary_{ts}.txt").write_text(text, encoding="utf-8")

    if "email" in channels and config.get("notify", "email", "enabled", default=False):
        _email_summary(config, text)


def _email_summary(config, text: str) -> None:
    ec = config.get("notify", "email", default={})
    pw = os.getenv(ec.get("smtp_password_env", "SUMMARY_SMTP_PASSWORD"), "")
    if not (ec.get("smtp_host") and ec.get("to") and pw):
        log.warning("Email summary enabled but SMTP not fully configured - skipping")
        return
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = "athr outreach - daily run summary"
    msg["From"] = ec.get("smtp_user", "")
    msg["To"] = ec["to"]
    try:
        with smtplib.SMTP(ec["smtp_host"], int(ec.get("smtp_port", 587))) as s:
            s.starttls()
            s.login(ec.get("smtp_user", ""), pw)
            s.send_message(msg)
        log.info("Summary emailed to %s", ec["to"])
    except Exception as exc:
        log.warning("Failed to email summary: %s", exc)
