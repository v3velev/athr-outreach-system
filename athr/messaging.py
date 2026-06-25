"""Message generation - write the Bulgarian outreach email per lead.

Hybrid personalization (as specced):
  * RULES pick the angle from the job's sector (config.sector_angles).
  * The AI writes the actual Bulgarian sentence around that angle + the live
    job facts (role, city, company, contact name).

Two providers:
  * AnthropicProvider - real Claude Messages API (x-api-key, /v1/messages).
  * MockProvider      - deterministic Bulgarian template, no key needed.

Both honour the voice/deliverability rules in config.messaging.rules and never
emit forbidden chars (em/en dashes) - a final clean() pass guarantees it.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .models import Contact, Job, Message
from .utils import get_logger

log = get_logger()

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class MessageProvider:
    live = False

    def generate(self, job: Job, contact: Contact) -> Optional[Message]:
        raise NotImplementedError


def _angle_for(config, job: Job) -> str:
    angles = config.get("sector_angles", default={}) or {}
    return angles.get(job.sector) or angles.get("default", "")


def _oneline(s: str, limit: int = 0) -> str:
    """Collapse ALL whitespace (newlines, tabs, runs of spaces) to single
    spaces and strip. Scraped titles/companies can contain newlines/control
    chars that would otherwise corrupt the email body or subject line.
    If limit>0, clip to that length (scraped fields can be absurdly long)."""
    if not s:
        return ""
    out = " ".join(s.split())
    if limit and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _short_role(title: str, limit: int = 38) -> str:
    """Trim a long job title to a clean, subject-friendly fragment.

    Many postings have titles like 'Помощник готвач от 5:30 до 14:30,
    понеделник-петък' - we cut at the first separator and cap the length so
    subject lines stay short (better deliverability + readability).
    """
    title = _oneline(title)
    if not title:
        return "позиция"
    head = re.split(r"[,/(]| от | с | за ", title, maxsplit=1)[0].strip()
    head = head or title
    return (head[: limit - 1].rstrip() + "…") if len(head) > limit else head


def _subject(config, job: Job, idx: int = 0) -> str:
    templates = config.get("messaging", "subjects", default=["{role} - {city}"])
    tpl = templates[idx % len(templates)]
    subj = tpl.format(role=_short_role(job.title), city=_oneline(job.city),
                      company=_oneline(job.company_name)).strip(" -")
    # a subject must be a single line, capped to a sane length
    return _oneline(clean(subj))[:90]


# --------------------------------------------------------------------------
class AnthropicProvider(MessageProvider):
    live = True

    def __init__(self, config, http):
        self.config = config
        self.http = http
        self.api_key = config.anthropic_key
        m = config.get("messaging", default={})
        self.model = m.get("model", "claude-sonnet-4-6")
        self.rules = m.get("rules", [])
        self.sender_name = m.get("sender_name", "")
        self.sender_company = m.get("sender_company", "athr.")
        self.sender_title = m.get("sender_title", "")

    def generate(self, job: Job, contact: Contact) -> Optional[Message]:
        angle = _angle_for(self.config, job)
        system = self._system_prompt()
        user = self._user_prompt(job, contact, angle)
        body = {
            "model": self.model,
            "max_tokens": 600,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        data = self.http.post_json(ANTHROPIC_URL, body, headers)
        if not data or data.get("type") == "error":
            if data:
                log.warning("Anthropic error: %s", data.get("error"))
            return None
        # content is a list of blocks; grab the first text block (not always [0])
        text = ""
        for block in (data.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
        if not text:
            return None
        subject, msg = _parse_subject_body(text)
        if not msg:
            return None
        return Message(subject=clean(subject) or _subject(self.config, job),
                       body=clean(msg), angle=angle, generator="anthropic")

    def _system_prompt(self) -> str:
        rules = "\n".join(f"- {r}" for r in self.rules)
        return (
            f"Ти си {self.sender_name}, {self.sender_title} на {self.sender_company} "
            f"- лицензирана агенция за подбор на персонал в България. Пишеш кратки, "
            f"човешки студени имейли до работодатели, които точно сега наемат хора.\n"
            f"Правила:\n{rules}\n"
            f"Върни ОТГОВОРА само като JSON: "
            f'{{"subject": "...", "body": "..."}} без друг текст.'
        )

    def _user_prompt(self, job: Job, contact: Contact, angle: str) -> str:
        greeting_name = _oneline(contact.first_name, 40)
        return (
            f"Компания: {_oneline(job.company_name, 70)}\n"
            f"Обява за: {_oneline(job.title, 90)}\n"
            f"Град: {_oneline(job.city, 40)}\n"
            f"Сектор/ъгъл за пина: {angle}\n"
            f"Лице за контакт (име за поздрав): {greeting_name}\n\n"
            f"Напиши имейла. Започни с '{greeting_name},' ако има име, иначе 'Здравейте,'. "
            f"Свържи се с конкретната обява и предложи, че бързо можем да запълним точно "
            f"тази роля. Завърши с подпис на нов ред: {self.sender_name}, {self.sender_company}."
        )


# --------------------------------------------------------------------------
class MockProvider(MessageProvider):
    """Rule-based Bulgarian template - realistic output with zero API cost."""
    live = False

    def __init__(self, config):
        self.config = config
        m = config.get("messaging", default={})
        self.sender_name = m.get("sender_name", "Ася Косаджиева")
        self.sender_company = m.get("sender_company", "athr.")

    # CTA variants so multiple contacts at one company don't get identical mail
    CTAS = [
        "Имате ли 10 минути този или следващия ден за кратък разговор?",
        "Удобно ли е да чуем за нуждите Ви накратко тази седмица?",
        "Само отговорете на този имейл и ще Ви върна подходящи кандидати.",
    ]

    def generate(self, job: Job, contact: Contact) -> Optional[Message]:
        angle = _angle_for(self.config, job)
        first = _oneline(contact.first_name, 40)
        greet = f"{first}," if first else "Здравейте,"
        role = _oneline(job.title, 90) or "позицията"
        city = f" в {_oneline(job.city, 40)}" if job.city else ""
        company = _oneline(job.company_name, 70)
        # deterministic per-recipient variation (stable across re-runs)
        idx = sum(ord(c) for c in (contact.email or contact.first_name or "x"))
        cta = self.CTAS[idx % len(self.CTAS)]
        body = (
            f"{greet}\n\n"
            f"Видях, че {company} в момента търси {role}{city}. "
            f"Ние от {self.sender_company} сме лицензирана агенция за подбор и "
            f"често осигуряваме {angle}.\n\n"
            f"Можем да поемем точно тази обява и да Ви предложим подходящи кандидати "
            f"бързо, без да губите време в пресяване. {cta}\n\n"
            f"Поздрави,\n{self.sender_name}\n{self.sender_company}"
        )
        return Message(subject=_subject(self.config, job, idx), body=clean(body),
                       angle=angle, generator="mock")


def build_message_provider(config, http) -> MessageProvider:
    if config.messaging_live:
        log.info("Messaging: LIVE (Anthropic %s)", config.get("messaging", "model"))
        return AnthropicProvider(config, http)
    log.info("Messaging: MOCK (no ANTHROPIC_API_KEY) - rule-based BG template")
    return MockProvider(config)


# ---- helpers ----
def _parse_subject_body(text: str) -> tuple[str, str]:
    """Accept a JSON {subject, body} or fall back to raw text as body."""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj.get("subject", ""), obj.get("body", "")
        except json.JSONDecodeError:
            pass
    return "", text


_DASH_RE = re.compile(r"[‒–—―]|--")


def clean(text: str) -> str:
    """Enforce the hard formatting rules: no em/en dashes or double hyphens."""
    if not text:
        return text
    text = _DASH_RE.sub("-", text)
    # collapse any accidental triple+ newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
