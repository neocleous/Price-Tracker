"""Aggregate observations into per-component lows, the cheapest full set,
all-time lows, and alert events."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from .config import Product
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Best:
    group_id: str
    source_id: str
    seller: str
    url: str
    landed: float
    landed_no_refund: float
    sticker: float
    currency: str
    in_stock: bool | None
    country: str
    vat_refund: str
    fetched_at: str
    shipping: float = 0.0  # reporting currency, as included in `landed`

    def as_dict(self) -> dict:
        return {
            "source": self.source_id,
            "seller": self.seller,
            "url": self.url,
            "landed": round(self.landed, 2),
            "landed_no_refund": round(self.landed_no_refund, 2),
            "sticker": round(self.sticker, 2),
            "currency": self.currency,
            "in_stock": self.in_stock,
            "country": self.country,
            "vat_refund": self.vat_refund,
            "fetched_at": self.fetched_at,
            "shipping": round(self.shipping, 2),
        }


@dataclass
class Snapshot:
    """Everything the MQTT publisher needs for one product."""

    product_id: str
    ts: str
    components: dict[str, dict[str, Any]] = field(default_factory=dict)  # cid -> {lowest, lowest_home, sources{}}
    bundles: dict[str, dict[str, Any]] = field(default_factory=dict)
    total: dict[str, Any] = field(default_factory=dict)
    alltime: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    fx: dict[str, float] = field(default_factory=dict)


def _row_to_best(row, product: Product, group_id: str, country_lookup: dict) -> Best:
    src = country_lookup[(group_id, row["source_id"])]
    return Best(
        group_id=group_id,
        source_id=row["source_id"],
        seller=row["seller"] or src.seller,
        url=row["url"] or src.url,
        landed=float(row["landed"]),
        landed_no_refund=float(row["landed_no_refund"]),
        sticker=float(row["sticker"]),
        currency=row["currency"],
        in_stock=None if row["in_stock"] is None else bool(row["in_stock"]),
        country=src.country,
        vat_refund=src.vat_refund,
        fetched_at=row["ts"],
        shipping=float(row["shipping"] or 0.0) if "shipping" in row.keys() else 0.0,
    )


def build_snapshot(product: Product, store: Store, fx_rates: dict[str, float], run_stats: dict,
                   new_obs_ids: set[int] | None = None, as_of: dt.datetime | None = None,
                   record: bool = True) -> Snapshot:
    """Aggregate the latest observations. With ``as_of`` the view is computed from
    observations up to that moment (history backfill); ``record=False`` skips
    the all-time-low bookkeeping and event generation."""
    now = as_of or dt.datetime.now(dt.timezone.utc)
    as_of_iso = now.isoformat() if as_of else None
    stale_cutoff = (now - dt.timedelta(hours=product.alerts.stale_after_hours)).isoformat()
    home = product.imports.home_country
    lookup = {(gid, s.id): s for gid, _b, _g, s in product.all_sources()}
    # disabled sources still need a lookup entry if old observations exist
    for grp in (*product.components, *product.bundles):
        for s in grp.sources:
            lookup.setdefault((grp.id, s.id), s)

    snap = Snapshot(product_id=product.id, ts=now.replace(microsecond=0).isoformat(), fx=fx_rates)

    def _group_view(group_id: str) -> tuple[dict[str, Any], Best | None, Best | None]:
        rows = store.latest_per_source(product.id, group_id, ok_only=True, as_of=as_of_iso)
        sources_view: dict[str, Any] = {}
        eligible: list[Best] = []
        for sid, row in rows.items():
            if (group_id, sid) not in lookup:
                continue
            b = _row_to_best(row, product, group_id, lookup)
            fresh = row["ts"] >= stale_cutoff
            sources_view[sid] = {**b.as_dict(), "fresh": fresh}
            if not fresh:
                continue
            if product.alerts.require_in_stock and b.in_stock is False:
                continue
            eligible.append(b)
        lowest = min(eligible, key=lambda b: b.landed, default=None)
        lowest_home = min((b for b in eligible if b.country == home), key=lambda b: b.landed, default=None)
        view = {
            "lowest": lowest.as_dict() if lowest else None,
            "lowest_home": lowest_home.as_dict() if lowest_home else None,
            "sources": sources_view,
        }
        return view, lowest, lowest_home

    comp_best: dict[str, Best | None] = {}
    comp_best_home: dict[str, Best | None] = {}
    for c in product.components:
        view, lo, lo_home = _group_view(c.id)
        snap.components[c.id] = view
        comp_best[c.id] = lo
        comp_best_home[c.id] = lo_home

    bundle_best: dict[str, Best | None] = {}
    for b in product.bundles:
        view, lo, _ = _group_view(b.id)
        snap.bundles[b.id] = view
        bundle_best[b.id] = lo

    snap.total = _cheapest_set(product, comp_best, bundle_best, label="total")
    snap.total["home"] = _cheapest_set(product, comp_best_home, {}, label="total_home")

    if record:
        snap.events = _events_and_alltime(product, store, snap, comp_best, bundle_best, new_obs_ids or set())
    snap.health = {
        **run_stats,
        "stale_after_hours": product.alerts.stale_after_hours,
    }
    return snap


def health_for(product: Product, snap: Snapshot, fetched_this_run: int = 0,
               failed_this_run: dict | None = None, run_id: int | None = None) -> dict:
    """Source health = enabled sources that have a fresh, successful observation."""
    fresh_ok = {
        (gid, sid)
        for gid, view in {**snap.components, **snap.bundles}.items()
        for sid, s in view["sources"].items()
        if s.get("fresh")
    }
    enabled = {(gid, src.id) for gid, _b, _g, src in product.all_sources()}
    return {
        "sources_total": len(enabled),
        "sources_ok": len(enabled & fresh_ok),
        "fetched_this_run": fetched_this_run,
        "failed_this_run": failed_this_run or {},
        "without_fresh_price": sorted(f"{g}/{s}" for g, s in enabled - fresh_ok),
        "problem": bool(enabled) and not (enabled & fresh_ok),
        "run_id": run_id,
        "stale_after_hours": product.alerts.stale_after_hours,
    }


def _cheapest_set(product: Product, comp_best: dict[str, Best | None], bundle_best: dict[str, Best | None], label: str) -> dict:
    """Cheapest way to obtain every required component: individual lows, or a
    bundle plus individual lows for whatever it does not cover. Bundles may be
    combined if they cover disjoint component sets."""
    required = [c.id for c in product.components if c.required]
    if not required:
        return {"value": None, "complete": False, "items": []}
    src_lookup = {(g.id, s.id): s for g in (*product.components, *product.bundles) for s in g.sources}

    def _plan(bundle_ids: tuple[str, ...]) -> tuple[float, list[dict], bool] | None:
        covered: set[str] = set()
        items: list[dict] = []
        total = 0.0
        for bid in bundle_ids:
            b = next(x for x in product.bundles if x.id == bid)
            if covered & set(b.covers):
                return None
            best = bundle_best.get(bid)
            if best is None:
                return None
            covered |= set(b.covers)
            total += best.landed
            items.append({"kind": "bundle", "id": bid, **best.as_dict()})
        complete = True
        for cid in required:
            if cid in covered:
                continue
            best = comp_best.get(cid)
            if best is None:
                complete = False
                items.append({"kind": "component", "id": cid, "missing": True})
                continue
            total += best.landed
            items.append({"kind": "component", "id": cid, **best.as_dict()})
        # shipping is per order: count it once per seller/session, not once per item
        by_seller: dict[tuple, list[float]] = {}
        for it in items:
            if it.get("missing"):
                continue
            src = src_lookup.get((it["id"], it["source"]))
            if src is None or not product.imports.route_for(src.country, src.route).shipping_per_order:
                continue
            by_seller.setdefault((src.seller, src.session), []).append(it.get("shipping") or 0.0)
        saved = sum(sum(v) - max(v) for v in by_seller.values() if len(v) > 1)
        total -= saved
        if saved:
            items.append({"kind": "adjustment", "id": "shipping_once_per_order", "landed": round(-saved, 2)})
        return total, items, complete

    plans = []
    bids = [b.id for b in product.bundles if bundle_best.get(b.id)]
    for r in range(0, len(bids) + 1):
        for combo in combinations(bids, r):
            p = _plan(combo)
            if p:
                plans.append(p)
    # prefer complete plans, then lowest total
    plans.sort(key=lambda p: (not p[2], p[0]))
    total, items, complete = plans[0]
    return {"value": round(total, 2) if complete else None, "partial_value": round(total, 2), "complete": complete, "items": items}


def _events_and_alltime(product, store: Store, snap: Snapshot, comp_best, bundle_best, new_obs_ids: set[int]) -> list[dict]:
    events: list[dict] = []
    pid = product.id
    alltime: dict[str, Any] = {}
    routes = {(g.id, s.id): s.route for g in (*product.components, *product.bundles) for s in g.sources}

    def _check_low(key: str, label: str, best: Best | None, value: float | None, extra: dict | None = None):
        prev = store.get_state(f"{pid}/alltime/{key}")
        if value is None:
            alltime[key] = prev
            return
        if prev is None or value < float(prev["value"]) - 0.005:
            rec = {"value": round(value, 2), "ts": snap.ts, **(best.as_dict() if best else {}), **(extra or {})}
            store.set_state(f"{pid}/alltime/{key}", rec)
            alltime[key] = rec
            if prev is not None:  # first observation is a baseline, not an alert
                events.append(
                    {
                        "event_type": "new_low",
                        "product": pid,
                        "scope": key,
                        "label": label,
                        "value": round(value, 2),
                        "previous": round(float(prev["value"]), 2),
                        "currency": product.imports.reporting_currency,
                        **({"seller": best.seller, "url": best.url, "country": best.country,
                            "delivery": product.imports.delivery_note(best.country, routes.get((best.group_id, best.source_id)))} if best else {}),
                    }
                )
        else:
            alltime[key] = prev

    for c in product.components:
        b = comp_best.get(c.id)
        _check_low(c.id, c.name, b, b.landed if b else None)
    for bd in product.bundles:
        b = bundle_best.get(bd.id)
        _check_low(bd.id, bd.name, b, b.landed if b else None)
    _check_low("total", f"{product.name} — complete set", None, snap.total.get("value"), {"items": snap.total.get("items")})
    snap.alltime = alltime

    # per-source price drops / back in stock, relative to the previous ok observation of that source
    thr = product.alerts.price_drop_pct / 100.0
    names = {g.id: g.name for g in (*product.components, *product.bundles)}
    countries = {(g.id, s.id): s.country for g in (*product.components, *product.bundles) for s in g.sources}
    for obs_id in sorted(new_obs_ids):
        row = store.observation(obs_id)
        if row is None or not row["ok"]:
            continue
        grp_id, sid = row["group_id"], row["source_id"]
        label = names.get(grp_id, grp_id)
        prev = store.previous_ok(pid, grp_id, sid, row["id"])
        if prev is None:
            continue
        cur_l, prev_l = float(row["landed"]), float(prev["landed"])
        country = countries.get((grp_id, sid), "")
        base = {
            "product": pid,
            "scope": grp_id,
            "label": label,
            "seller": row["seller"],
            "url": row["url"],
            "value": round(cur_l, 2),
            "currency": product.imports.reporting_currency,
            "country": country,
            "delivery": product.imports.delivery_note(country, routes.get((grp_id, sid))) if country else "",
        }
        if prev_l > 0 and (prev_l - cur_l) / prev_l >= thr:
            events.append(
                {
                    "event_type": "price_drop",
                    **base,
                    "previous": round(prev_l, 2),
                    "pct": round((prev_l - cur_l) / prev_l * 100, 1),
                }
            )
        if prev["in_stock"] == 0 and row["in_stock"] == 1:
            events.append({"event_type": "back_in_stock", **base})
    return events
