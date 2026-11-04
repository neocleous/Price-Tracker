"""One tracking run for one product: fetch due sources → parse → landed cost →
store → aggregate → publish."""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor

from .compute import Snapshot, build_snapshot
from .config import Product, Settings, Source
from .fetch import Fetcher, FetchError
from .fx import FX
from .landed import compute_landed
from .mqtt import Publisher, _default_currency
from .parse import ParseError, extract, page_title
from .store import Store

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, settings: Settings, store: Store, publisher: Publisher | None):
        self.settings = settings
        self.store = store
        self.publisher = publisher

    # ------------------------------------------------------------------ one source
    def observe(self, product: Product, group_id: str, is_bundle: bool, group, source: Source,
                fetcher: Fetcher, fx: FX) -> tuple[int, str | None]:
        """Fetch + parse one source; always records an observation row. Returns (obs_id, error)."""
        currency_default = source.currency or _default_currency(source.country)
        base = dict(product_id=product.id, group_id=group_id, is_bundle=int(is_bundle),
                    source_id=source.id, seller=source.seller, url=source.url)
        try:
            html = fetcher.get(source)
            offer = extract(
                html, source.parser, default_currency=currency_default, match=source.match,
                selector=source.selector, pattern=source.pattern,
                stock_selector=source.stock_selector, stock_in_regex=source.stock_in_regex,
            )
            currency = (offer.currency or currency_default or product.imports.reporting_currency).upper()
            rate = fx.rate(currency)
            landed = compute_landed(offer.price, currency, source, product.imports, rate)
            lo, hi = group.bounds.min, group.bounds.max
            if not (lo <= landed.landed <= hi):
                raise ParseError(
                    f"price {offer.price:.2f} {currency} → landed {landed.landed:.0f} {product.imports.reporting_currency} "
                    f"outside bounds [{lo:.0f}, {hi:.0f}] (method={offer.method}, name={offer.name!r})"
                )
            obs_id = self.store.add_observation(
                **base, ok=1, sticker=offer.price, currency=currency, landed=landed.landed,
                landed_no_refund=landed.landed_no_refund,
                in_stock=None if offer.in_stock is None else int(offer.in_stock),
                fx_rate=rate, method=offer.method, title=(offer.name or page_title(html) or "")[:200], sku=offer.sku,
            )
            log.info("%s/%s: %.2f %s → %.0f %s (%s, stock=%s)", group_id, source.id, offer.price, currency,
                     landed.landed, product.imports.reporting_currency, offer.method, offer.in_stock)
            return obs_id, None
        except (FetchError, ParseError, RuntimeError) as e:
            msg = f"{type(e).__name__}: {e}"
            obs_id = self.store.add_observation(**base, ok=0, error=msg[:500])
            log.warning("%s/%s failed: %s", group_id, source.id, msg)
            return obs_id, msg

    # ------------------------------------------------------------------ one product
    def run_product(self, product: Product, force_all: bool = False, only: set[str] | None = None,
                    publish: bool = True, workers: int = 4) -> Snapshot:
        run_id = self.store.start_run(product.id)
        now = dt.datetime.now(dt.timezone.utc)
        fx = FX(self.store, product.imports.reporting_currency)
        fetcher = Fetcher(self.settings)
        due = []
        for gid, is_bundle, grp, src in product.all_sources():
            if only and src.id not in only and gid not in only:
                continue
            if not force_all:
                last = self.store.last_source_run(product.id, gid, src.id)
                # back off sources that keep failing: double the interval per 3 failures, max 24 h
                fails = self.store.consecutive_failures(product.id, gid, src.id)
                interval = min(src.interval_hours * (2 ** (fails // 3)), 24)
                if last and (now - last) < dt.timedelta(hours=interval) - dt.timedelta(minutes=5):
                    continue
            due.append((gid, is_bundle, grp, src))
        log.info("product %s: %d of %d sources due", product.id, len(due), sum(1 for _ in product.all_sources()))

        new_ids: set[int] = set()
        errors: dict[str, str] = {}
        try:
            # group work by host so per-host pacing is respected, parallel across hosts
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self.observe, product, gid, b, grp, src, fetcher, fx): (gid, src) for gid, b, grp, src in due}
                for fut, (gid, src) in futs.items():
                    obs_id, err = fut.result()
                    new_ids.add(obs_id)
                    if err:
                        errors[f"{gid}/{src.id}"] = err
        finally:
            fetcher.close()

        ok = len(due) - len(errors)
        self.store.end_run(run_id, len(due), ok)
        fx_rates = {c: fx.rate(c) for c in _currencies(product)}
        snap = build_snapshot(product, self.store, fx_rates, {}, new_ids)
        # health = sources with a fresh, successful observation (not just this run's due set)
        fresh_ok = {
            (gid, sid)
            for gid, view in {**snap.components, **snap.bundles}.items()
            for sid, s in view["sources"].items()
            if s.get("fresh")
        }
        enabled = {(gid, src.id) for gid, _b, _g, src in product.all_sources()}
        stale = sorted(f"{g}/{s}" for g, s in enabled - fresh_ok)
        snap.health = {
            "sources_total": len(enabled),
            "sources_ok": len(enabled & fresh_ok),
            "fetched_this_run": len(due),
            "failed_this_run": errors,
            "without_fresh_price": stale,
            "problem": bool(enabled) and not (enabled & fresh_ok),
            "run_id": run_id,
            "stale_after_hours": product.alerts.stale_after_hours,
        }
        if publish and self.publisher is not None:
            self.publisher.publish_snapshot(product, snap)
        return snap


def _currencies(product: Product) -> set[str]:
    out = set()
    for _g, _b, _grp, s in product.all_sources():
        c = s.currency or _default_currency(s.country)
        if c and c != product.imports.reporting_currency:
            out.add(c)
    return out
