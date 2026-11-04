"""HTTP fetching with polite per-host pacing, retries, and optional Playwright."""

from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urlsplit

import httpx

from .config import Settings, Source

log = logging.getLogger(__name__)

_ACCEPT_LANG = {
    "CH": "de-CH,de;q=0.9,en;q=0.7,fr;q=0.5",
    "DE": "de-DE,de;q=0.9,en;q=0.6",
    "AT": "de-AT,de;q=0.9,en;q=0.6",
    "FR": "fr-FR,fr;q=0.9,en;q=0.6",
    "IT": "it-IT,it;q=0.9,en;q=0.6",
    "NL": "nl-NL,nl;q=0.9,en;q=0.6",
    "GB": "en-GB,en;q=0.9",
}


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class Fetcher:
    """One instance per run; shares an httpx client and per-host last-hit times."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            http2=False,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "no-cache",
            },
        )
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self._pw = None

    def close(self):
        self._client.close()
        if self._pw is not None:
            try:
                self._pw_browser.close()
                self._pw.stop()
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ pacing
    def _pace(self, url: str):
        host = urlsplit(url).netloc
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = self.settings.per_host_delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.5))
            self._last_hit[host] = time.monotonic()

    # ------------------------------------------------------------------ http
    def get(self, source: Source) -> str:
        if source.parser == "playwright":
            return self._get_playwright(source)
        headers = {"Accept-Language": _ACCEPT_LANG.get(source.country, "en;q=0.9")}
        headers.update(source.headers)
        last_exc: Exception | None = None
        for attempt in range(3):
            self._pace(source.url)
            try:
                r = self._client.get(source.url, headers=headers)
            except httpx.HTTPError as e:
                last_exc = FetchError(f"{type(e).__name__}: {e}")
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = FetchError(f"HTTP {r.status_code}", r.status_code)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}", r.status_code)
            text = r.text
            if _looks_blocked(text):
                raise FetchError("bot challenge page returned", r.status_code)
            return text
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------ playwright
    def _get_playwright(self, source: Source) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise FetchError("playwright not installed (build image with WITH_PLAYWRIGHT=true)") from e
        if self._pw is None:
            self._pw = sync_playwright().start()
            self._pw_browser = self._pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        self._pace(source.url)
        ctx = self._pw_browser.new_context(
            user_agent=self.settings.user_agent,
            locale=_ACCEPT_LANG.get(source.country, "en-GB").split(",")[0],
            viewport={"width": 1366, "height": 900},
        )
        try:
            page = ctx.new_page()
            page.goto(source.url, wait_until="domcontentloaded", timeout=int(self.settings.request_timeout * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            if source.selector:
                try:
                    page.wait_for_selector(source.selector, timeout=10_000)
                except Exception:
                    pass
            html = page.content()
        finally:
            ctx.close()
        if _looks_blocked(html):
            raise FetchError("bot challenge page returned (playwright)")
        return html


_BLOCK_MARKERS = (
    "cf-browser-verification",
    "challenge-platform",
    "Just a moment...",
    "Access Denied",
    "Zugriff verweigert",
    "captcha-delivery.com",
    "px-captcha",
    "Pardon Our Interruption",
    "are you a robot",
    "Bitte bestätigen Sie, dass Sie kein Roboter",
)


def _looks_blocked(html: str) -> bool:
    head = html[:20000]
    return any(m.lower() in head.lower() for m in _BLOCK_MARKERS) and "application/ld+json" not in head
