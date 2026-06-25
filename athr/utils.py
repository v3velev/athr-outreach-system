"""Shared utilities: logging, a polite Chrome-impersonating HTTP client,
retry/backoff, and a simple rate limiter.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:  # pragma: no cover - allow import without curl_cffi for unit tests
    import requests as cffi_requests  # type: ignore
    _HAS_CFFI = False


def get_logger(name: str = "athr") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


log = get_logger()


class RateLimiter:
    """Sleep a random delay between calls so we stay polite (and unbanned)."""

    def __init__(self, delay_range: tuple[float, float] = (1.5, 3.5)):
        self.lo, self.hi = delay_range
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        target = self._last + random.uniform(self.lo, self.hi)
        if now < target:
            time.sleep(target - now)
        self._last = time.monotonic()


class HttpClient:
    """Thin wrapper around curl_cffi with Chrome impersonation + retries.

    Chrome TLS impersonation is what gets us past Cloudflare on jobs.bg, and it
    is harmless everywhere else. Falls back to plain requests if curl_cffi is
    not installed (e.g. in a minimal CI), losing only the Cloudflare bypass.
    """

    def __init__(self, impersonate: str = "chrome124",
                 delay_range: tuple[float, float] = (1.5, 3.5),
                 max_retries: int = 4):
        self.max_retries = max_retries
        self.limiter = RateLimiter(delay_range)
        if _HAS_CFFI:
            self.session = cffi_requests.Session(impersonate=impersonate)
        else:
            self.session = cffi_requests.Session()
            log.warning("curl_cffi not installed - Cloudflare-protected sources "
                        "(jobs.bg) will fail. Install curl_cffi for full coverage.")

    def get(self, url: str, timeout: int = 25, polite: bool = True,
            quick: bool = False) -> Optional[str]:
        """GET with retry/backoff. Returns response text or None on failure.

        quick=True: a single fast attempt, no retries/backoff and no politeness
        delay - for best-effort fetches like scraping arbitrary company websites
        (many are dead/slow; we must not block the whole run retrying them).
        """
        tries = 1 if quick else self.max_retries
        for attempt in range(tries):
            if polite and not quick:
                self.limiter.wait()
            try:
                resp = self.session.get(url, timeout=(8 if quick else timeout))
                status = resp.status_code
                if status == 200 and len(resp.text) > 500:
                    return resp.text
                if quick:
                    return resp.text if status == 200 else None
                # 403 + tiny body = Cloudflare block or soft rate-limit
                if status in (403, 429, 503) or len(resp.text) < 800:
                    backoff = min(2 ** attempt * 5, 60)
                    log.warning("  %s -> HTTP %s (len %d), retry in %ss",
                                _short(url), status, len(resp.text), backoff)
                    time.sleep(backoff)
                    continue
                return resp.text
            except Exception as exc:  # network error / DNS failure
                if quick:
                    return None
                backoff = min(2 ** attempt * 3, 45)
                log.warning("  %s -> %s, retry in %ss", _short(url), exc, backoff)
                time.sleep(backoff)
        if not quick:
            log.error("  GAVE UP on %s after %d attempts", _short(url), self.max_retries)
        return None

    def post_json(self, url: str, json_body: dict, headers: dict,
                  timeout: int = 30, max_retries: int = 5,
                  quiet_4xx: bool = False) -> Optional[dict]:
        """POST JSON with retry on 429/5xx. Returns parsed JSON or None.

        quiet_4xx: don't log 4xx responses as errors - for APIs where a 4xx is
        an expected outcome (e.g. Prospeo returns 400 NO_RESULTS when a company
        simply isn't in its database).
        """
        for attempt in range(max_retries):
            try:
                resp = self.session.post(url, json=json_body, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504):
                    backoff = min(2 ** attempt * 2, 60)
                    log.warning("  POST %s -> %s, retry in %ss",
                                _short(url), resp.status_code, backoff)
                    time.sleep(backoff)
                    continue
                if not quiet_4xx:
                    log.error("  POST %s -> HTTP %s: %s", _short(url),
                              resp.status_code, resp.text[:200])
                return None
            except Exception as exc:
                backoff = min(2 ** attempt * 2, 45)
                log.warning("  POST %s -> %s, retry in %ss", _short(url), exc, backoff)
                time.sleep(backoff)
        return None


def _short(url: str) -> str:
    return url if len(url) < 70 else url[:67] + "..."


import functools


@functools.lru_cache(maxsize=4096)
def domain_accepts_mail(domain: str) -> bool:
    """Best-effort check that an email domain can actually receive mail, to
    avoid sending to dead/typo domains (the worst bounce category, which wrecks
    sender reputation). Verified scraped emails aren't available from Prospeo,
    so this is our cheap first line of defence.

    Uses MX records via dnspython when installed, else falls back to a plain
    DNS resolve (catches non-existent domains). Fails OPEN on transient errors
    so a DNS hiccup never blocks a legitimate send.
    """
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return False
    try:
        import dns.resolver  # type: ignore
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5)
            return len(answers) > 0
        except dns.resolver.NXDOMAIN:
            return False                      # domain does not exist
        except dns.resolver.NoAnswer:
            pass                              # no MX -> maybe accepts on A record
        except Exception:
            return True                       # DNS timeout/hiccup -> don't block
    except ImportError:
        pass
    import socket
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False                          # does not resolve
    except Exception:
        return True                           # transient -> fail open


class RunLock:
    """Prevent two runs overlapping (e.g. cron fires while yesterday's run is
    still going) - which would have two processes writing the same SQLite file.

    A simple PID lockfile with stale detection. Use as a context manager:
        with RunLock(path) as ok:
            if not ok: ...exit...
    """

    def __init__(self, path):
        self.path = path
        self.acquired = False

    def __enter__(self) -> bool:
        import os
        if self.path.exists():
            try:
                old_pid = int(self.path.read_text().strip())
                os.kill(old_pid, 0)         # raises if process is gone
                return False                # another run is genuinely active
            except (ValueError, ProcessLookupError, PermissionError):
                pass                        # stale lock - take it over
        self.path.write_text(str(os.getpid()))
        self.acquired = True
        return True

    def __exit__(self, *exc):
        if self.acquired and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
