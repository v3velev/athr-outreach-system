"""Sending - push a finished lead + its personalised message to the outreach tool.

Two providers:
  * SmartLeadSender - real SmartLead API. Adds the lead to a campaign with the
    full message baked into custom_fields ({{subject}} / {{message}}), matching
    the proven "single merge-variable" pattern. The campaign sequence body is
    just {{message}} and subject {{subject}}, so each lead carries its own copy.
  * MockSender - appends to data/outbox.csv so you can review exactly what
    WOULD be sent, with zero side effects.

SmartLead API (project skill 'smartlead-api'):
  Base https://server.smartlead.ai/api/v1 ; auth ?api_key=
  POST /campaigns/{id}/leads
    body { lead_list:[{email, first_name, last_name, company_name, custom_fields}],
           settings:{...} }
  Returns { upload_count, already_added_to_campaign_count, invalid_email_count, ... }
  NOTE: upload_count can overstate by a few (silent drops); we log raw response.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .models import Contact, Job, Message
from .utils import get_logger

log = get_logger()

SMARTLEAD_BASE = "https://server.smartlead.ai/api/v1"


class Sender:
    live = False

    def send(self, job: Job, contact: Contact, message: Message) -> tuple[bool, str]:
        raise NotImplementedError

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
class SmartLeadSender(Sender):
    live = True

    def __init__(self, config, http):
        self.http = http
        self.api_key = config.smartlead_key
        self.campaign_id = config.smartlead_campaign_id
        self.client_id = config.smartlead_client_id

    def _url(self) -> str:
        url = f"{SMARTLEAD_BASE}/campaigns/{self.campaign_id}/leads?api_key={self.api_key}"
        if self.client_id:
            url += f"&client_id={self.client_id}"
        return url

    def send(self, job: Job, contact: Contact, message: Message) -> tuple[bool, str]:
        lead = {
            "email": contact.email,
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "company_name": job.company_name,
            "custom_fields": {
                "subject": message.subject,
                "message": message.body,
                "role": job.title,
                "city": job.city,
                "sector": job.sector,
                "job_url": job.url,
            },
        }
        payload = {
            "lead_list": [lead],
            "settings": {
                "ignore_global_block_list": False,
                "ignore_unsubscribe_list": False,
                "ignore_duplicate_leads_in_other_campaign": False,
            },
        }
        data = self.http.post_json(self._url(), payload,
                                   headers={"Content-Type": "application/json"})
        if not data:
            return False, "smartlead_no_response"
        uploaded = data.get("upload_count", 0) or 0
        already = data.get("already_added_to_campaign_count", 0) or 0
        if uploaded > 0:
            return True, f"smartlead_ok:{data}"
        if already > 0:
            # not a failure - the lead is already in the campaign (idempotent)
            return True, f"smartlead_already_present:{data}"
        if data.get("invalid_email_count"):
            return False, f"smartlead_invalid_email:{data}"
        return False, f"smartlead_not_added:{data}"


# --------------------------------------------------------------------------
class MockSender(Sender):
    """Writes leads to data/outbox.csv instead of sending. Inspect before going live."""
    live = False

    FIELDS = ["email", "first_name", "last_name", "company_name", "subject",
              "message", "role", "city", "sector", "country", "email_status",
              "contact_source", "msg_generator", "job_url"]

    def __init__(self, config):
        self.path: Path = config.data_dir / "outbox.csv"
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.FIELDS)
        if self.path.stat().st_size == 0:
            self._writer.writeheader()

    def send(self, job: Job, contact: Contact, message: Message) -> tuple[bool, str]:
        self._writer.writerow({
            "email": contact.email, "first_name": contact.first_name,
            "last_name": contact.last_name, "company_name": job.company_name,
            "subject": message.subject, "message": message.body,
            "role": job.title, "city": job.city, "sector": job.sector,
            "country": job.country, "email_status": contact.email_status,
            "contact_source": contact.source, "msg_generator": message.generator,
            "job_url": job.url,
        })
        self._fh.flush()
        return True, f"mock_csv:{self.path.name}"

    def close(self) -> None:
        self._fh.close()


def build_sender(config, http) -> Sender:
    if config.sending_live:
        log.info("Sending: LIVE (SmartLead campaign %s)", config.smartlead_campaign_id)
        return SmartLeadSender(config, http)
    why = "no SMARTLEAD_API_KEY" if not config.smartlead_key else "no SMARTLEAD_CAMPAIGN_ID"
    log.info("Sending: MOCK (%s) - writing to data/outbox.csv", why)
    return MockSender(config)
