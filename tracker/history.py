"""Cheapest-complete-set history: one snapshot per run, backfilled from past
observations when missing, plus a change log and the all-time low."""

from __future__ import annotations

import datetime as dt
import json
import logging

from .compute import build_snapshot
from .config import Product
from .store import Store

log = logging.getLogger(__name__)


def record_snapshot(store: Store, product: Product, snap, run_id: int | None = None):
    store.add_snapshot(product.id, snap.ts, snap.total.get("value"), snap.total.get("home", {}).get("value"),
                       snap.total.get("items", []), run_id)


def backfill(store: Store, product: Product) -> int:
    """Replay every completed run and store the cheapest set as it was then.
    Idempotent: only runs without a snapshot are replayed."""
    have = {r["run_id"] for r in store.snapshots(product.id) if r["run_id"] is not None}
    n = 0
    for run in store.runs(product.id):
        if run["id"] in have:
            continue
        ts = dt.datetime.fromisoformat(run["ts_end"])
        snap = build_snapshot(product, store, {}, {}, as_of=ts, record=False)
        store.add_snapshot(product.id, run["ts_end"], snap.total.get("value"),
                           snap.total.get("home", {}).get("value"), snap.total.get("items", []), run["id"])
        n += 1
    if n:
        log.info("backfilled %d history snapshots for %s", n, product.id)
    return n


def series(store: Store, product: Product, days: int | None = None) -> dict:
    """Time series + change log + all-time low, ready for a chart or the API."""
    rows = store.snapshots(product.id, days)
    points = [{"ts": r["ts"], "total": r["total"], "home": r["total_home"]} for r in rows]
    changes = []
    prev = None
    for r in rows:
        tot = r["total"]
        if tot is None:
            continue
        if prev is not None and abs(tot - prev["total"]) > 0.005:
            items = json.loads(r["items"] or "[]")
            prev_items = json.loads(prev["items"] or "[]")
            changes.append({
                "ts": r["ts"], "from": round(prev["total"], 2), "to": round(tot, 2),
                "delta": round(tot - prev["total"], 2),
                "drivers": _diff(prev_items, items),
            })
        prev = r
    low = None
    for r in rows:
        if r["total"] is not None and (low is None or r["total"] < low["total"] - 0.005):
            low = r
    alltime = None
    if low is not None:
        alltime = {"ts": low["ts"], "total": round(low["total"], 2), "items": json.loads(low["items"] or "[]")}
    return {"points": points, "changes": list(reversed(changes)), "alltime": alltime,
            "current": points[-1] if points else None}


def _diff(before: list[dict], after: list[dict]) -> list[str]:
    """Human-readable reasons a total moved: per component/bundle id, what changed."""
    b = {i.get("id"): i for i in before if not i.get("missing")}
    a = {i.get("id"): i for i in after if not i.get("missing")}
    out = []
    for key in sorted(set(a) | set(b)):
        x, y = b.get(key), a.get(key)
        if (x or y or {}).get("kind") == "adjustment":
            xv, yv = (x or {}).get("landed", 0) or 0, (y or {}).get("landed", 0) or 0
            if abs(xv - yv) > 0.005:
                out.append(f"shipping counted once per order: {xv:.2f} → {yv:.2f}")
            continue
        if x is None and y is not None:
            out.append(f"{key}: now from {y.get('seller', '?')} at {y.get('landed')}")
        elif y is None and x is not None:
            out.append(f"{key}: no longer used (was {x.get('seller', '?')} at {x.get('landed')})")
        elif x and y and (x.get("seller") != y.get("seller") or abs((x.get("landed") or 0) - (y.get("landed") or 0)) > 0.005):
            if x.get("seller") == y.get("seller"):
                out.append(f"{key}: {y.get('seller')} {x.get('landed')} → {y.get('landed')}")
            else:
                out.append(f"{key}: {x.get('seller')} {x.get('landed')} → {y.get('seller')} {y.get('landed')}")
    return out
