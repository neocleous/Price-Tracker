"""ECB reference rates via frankfurter.app, cached in SQLite (one call per day)."""

from __future__ import annotations

import datetime as dt
import logging

import httpx

from .store import Store

log = logging.getLogger(__name__)

API = "https://api.frankfurter.dev/v1/latest"
API_FALLBACK = "https://api.frankfurter.app/latest"


class FX:
    def __init__(self, store: Store, quote: str):
        self.store = store
        self.quote = quote.upper()
        self._mem: dict[str, float] = {}

    def rate(self, base: str) -> float:
        """Units of ``quote`` per 1 unit of ``base``."""
        base = base.upper()
        if base == self.quote:
            return 1.0
        if base in self._mem:
            return self._mem[base]
        today = dt.date.today().isoformat()
        cached = self.store.get_fx(today, base, self.quote)
        if cached is None:
            cached = self._fetch(base)
            if cached is not None:
                self.store.put_fx(today, base, self.quote, cached)
        if cached is None:
            cached = self.store.latest_fx(base, self.quote)
            if cached is None:
                raise RuntimeError(f"no FX rate available for {base}->{self.quote}")
            log.warning("using stale FX rate for %s->%s: %.4f", base, self.quote, cached)
        self._mem[base] = cached
        return cached

    def _fetch(self, base: str) -> float | None:
        for url in (API, API_FALLBACK):
            try:
                r = httpx.get(url, params={"base": base, "symbols": self.quote}, timeout=15, follow_redirects=True)
                r.raise_for_status()
                data = r.json()
                return float(data["rates"][self.quote])
            except Exception as e:  # noqa: BLE001
                log.warning("FX fetch failed from %s: %s", url, e)
        return None
