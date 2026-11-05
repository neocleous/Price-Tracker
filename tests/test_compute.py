import datetime as dt

import pytest

from tracker.compute import build_snapshot
from tracker.config import Product
from tracker.store import Store

PRODUCT = {
    "id": "demo",
    "name": "Demo",
    "imports": {
        "home_country": "CH",
        "reporting_currency": "CHF",
        "import_vat": 0.081,
        "routes": {"DE": {"mode": "collect", "local_vat": 0.19}},
        "default_route": {"mode": "ship", "local_vat": 0.2, "clearance_fee": 25},
    },
    "alerts": {"price_drop_pct": 5, "require_in_stock": True, "stale_after_hours": 36},
    "components": [
        {"id": "frame", "name": "Frame", "bounds": {"min": 100, "max": 5000}, "sources": [
            {"id": "ch1", "seller": "CH Shop", "url": "https://ch1", "country": "CH"},
            {"id": "de1", "seller": "DE Shop", "url": "https://de1", "country": "DE", "vat_refund": "yes"},
        ]},
        {"id": "seat", "name": "Seat", "bounds": {"min": 50, "max": 2000}, "sources": [
            {"id": "ch1", "seller": "CH Shop", "url": "https://ch1s", "country": "CH"},
        ]},
    ],
    "bundles": [
        {"id": "frame_seat", "name": "Frame+Seat", "covers": ["frame", "seat"], "bounds": {"min": 100, "max": 9000},
         "sources": [{"id": "ch1", "seller": "CH Shop", "url": "https://ch1b", "country": "CH"}]},
    ],
}


def obs(store, gid, sid, landed, in_stock=1, ok=1, bundle=0, ts=None, **kw):
    return store.add_observation(product_id="demo", group_id=gid, is_bundle=bundle, source_id=sid, seller=sid, url="u",
                                 ok=ok, sticker=landed, currency="CHF", landed=landed, landed_no_refund=landed,
                                 in_stock=in_stock, fx_rate=1.0, method="t", **({"ts": ts} if ts else {}), **kw)


def test_lowest_and_home_and_total():
    p = Product.model_validate(PRODUCT)
    s = Store(":memory:")
    obs(s, "frame", "ch1", 1000)
    obs(s, "frame", "de1", 900)
    obs(s, "seat", "ch1", 400)
    snap = build_snapshot(p, s, {"EUR": 0.94}, {})
    assert snap.components["frame"]["lowest"]["source"] == "de1"
    assert snap.components["frame"]["lowest_home"]["source"] == "ch1"
    assert snap.total["value"] == 1300 and snap.total["complete"] is True
    assert snap.total["home"]["value"] == 1400


def test_bundle_beats_individual_when_cheaper():
    p = Product.model_validate(PRODUCT)
    s = Store(":memory:")
    obs(s, "frame", "ch1", 1000)
    obs(s, "seat", "ch1", 400)
    obs(s, "frame_seat", "ch1", 1250, bundle=1)
    snap = build_snapshot(p, s, {}, {})
    assert snap.total["value"] == 1250
    assert snap.total["items"][0]["kind"] == "bundle"


def test_out_of_stock_and_stale_excluded():
    p = Product.model_validate(PRODUCT)
    s = Store(":memory:")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)).isoformat()
    obs(s, "frame", "de1", 800, ts=old)  # stale
    obs(s, "frame", "ch1", 950, in_stock=0)  # out of stock
    snap = build_snapshot(p, s, {}, {})
    assert snap.components["frame"]["lowest"] is None
    assert snap.components["frame"]["sources"]["de1"]["fresh"] is False
    assert snap.total["value"] is None and snap.total["complete"] is False


def test_events_new_low_and_price_drop():
    p = Product.model_validate(PRODUCT)
    s = Store(":memory:")
    i1 = obs(s, "frame", "ch1", 1000)
    i2 = obs(s, "seat", "ch1", 400)
    snap1 = build_snapshot(p, s, {}, {}, {i1, i2})
    assert snap1.events == []  # first run = baseline
    assert snap1.alltime["total"]["value"] == 1400
    i3 = obs(s, "frame", "ch1", 900)  # 10% drop
    snap2 = build_snapshot(p, s, {}, {}, {i3})
    types = sorted(e["event_type"] for e in snap2.events)
    assert types.count("new_low") == 2  # frame + total
    assert "price_drop" in types
    drop = next(e for e in snap2.events if e["event_type"] == "price_drop")
    assert drop["pct"] == 10.0 and drop["previous"] == 1000
    # same price again → nothing
    i4 = obs(s, "frame", "ch1", 900)
    assert build_snapshot(p, s, {}, {}, {i4}).events == []


def test_back_in_stock_event():
    p = Product.model_validate(PRODUCT)
    s = Store(":memory:")
    obs(s, "frame", "ch1", 1000, in_stock=0)
    i = obs(s, "frame", "ch1", 1000, in_stock=1)
    snap = build_snapshot(p, s, {}, {}, {i})
    assert any(e["event_type"] == "back_in_stock" for e in snap.events)


def test_shipping_counted_once_per_seller():
    cfg = {**PRODUCT, "imports": {**PRODUCT["imports"], "routes": {"ddp": {"mode": "ddp", "default_shipping": 12.95}}}}
    cfg["components"] = [
        {"id": "frame", "name": "Frame", "bounds": {"min": 100, "max": 5000}, "sources": [
            {"id": "shop", "seller": "Shop", "url": "u1", "country": "DE", "route": "ddp", "currency": "CHF"}]},
        {"id": "seat", "name": "Seat", "bounds": {"min": 50, "max": 2000}, "sources": [
            {"id": "shop", "seller": "Shop", "url": "u2", "country": "DE", "route": "ddp", "currency": "CHF"}]},
    ]
    cfg["bundles"] = []
    p = Product.model_validate(cfg)
    s = Store(":memory:")
    obs(s, "frame", "shop", 1012.95, shipping=12.95)
    obs(s, "seat", "shop", 212.95, shipping=12.95)
    snap = build_snapshot(p, s, {}, {})
    assert snap.total["value"] == pytest.approx(1212.95)  # 1000 + 200 + one shipping
    assert any(i["kind"] == "adjustment" for i in snap.total["items"])
