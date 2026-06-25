"""Configuration loading: merges config.yaml (behaviour) + .env (secrets).

Exposes a single Config object. Determines, per stage, whether we run LIVE
(real API key present) or MOCK (no key) - this is what lets the whole system
run end-to-end before any real credentials are entered.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv optional; fall back to plain environment
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path

    # secrets (may be empty -> mock mode for that stage)
    prospeo_key: str = ""
    anthropic_key: str = ""
    smartlead_key: str = ""
    smartlead_campaign_id: str = ""
    smartlead_client_id: str = ""
    parallel_key: str = ""
    trykitt_key: str = ""

    # ---- convenience accessors into the yaml tree ----
    def get(self, *path, default=None):
        node = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def data_dir(self) -> Path:
        d = self.root / self.get("run", "data_dir", default="data")
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- mode flags: live vs mock per stage ----
    @property
    def enrich_live(self) -> bool:
        return bool(self.prospeo_key)

    @property
    def messaging_live(self) -> bool:
        return bool(self.anthropic_key)

    @property
    def sending_live(self) -> bool:
        return bool(self.smartlead_key and self.smartlead_campaign_id)

    def mode_banner(self) -> str:
        def tag(live: bool) -> str:
            return "LIVE" if live else "MOCK"
        return (
            f"enrich={tag(self.enrich_live)}  "
            f"messaging={tag(self.messaging_live)}  "
            f"sending={tag(self.sending_live)}"
        )


def load_config(config_path: str | None = None, env_path: str | None = None) -> Config:
    """Load config.yaml + .env into a Config object."""
    load_dotenv(env_path or (ROOT / ".env"))

    cfg_file = Path(config_path) if config_path else (ROOT / "config.yaml")
    with open(cfg_file, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    return Config(
        raw=raw,
        root=ROOT,
        prospeo_key=os.getenv("PROSPEO_API_KEY", "").strip(),
        anthropic_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        smartlead_key=os.getenv("SMARTLEAD_API_KEY", "").strip(),
        smartlead_campaign_id=os.getenv("SMARTLEAD_CAMPAIGN_ID", "").strip(),
        smartlead_client_id=os.getenv("SMARTLEAD_CLIENT_ID", "").strip(),
        parallel_key=os.getenv("PARALLEL_API_KEY", "").strip(),
        trykitt_key=os.getenv("TRYKITT_API_KEY", "").strip(),
    )
