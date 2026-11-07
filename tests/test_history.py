import datetime as dt

from tracker.config import Product
from tracker.history import backfill, series
from tracker.store import Store

from test_compute import PRODUCT, obs


def test_backfill_and_series():
    p = Product.model_validate(PRODUCT)
    s = Store(":memory:")
    t0 = dt.datetime(2026, 11, 5, 8, 0, tzinfo=dt.timezone.utc)
    # run 1: frame 1000 + seat 400
    r1 = s.start_run(p.id)
    obs(s, "frame", "ch1", 1000, ts=t0.isoformat()); obs(s, "seat", "ch1", 400, ts=t0.isoformat())
    s.db.execute("UPDATE runs SET ts_end=?, sources_total=2, sources_ok=2 WHERE id=?", ((t0 + dt.timedelta(minutes=1)).isoformat(), r1)); s.db.commit()
    # run 2: frame drops to 900 at de1
    t1 = t0 + dt.timedelta(hours=1)
    r2 = s.start_run(p.id)
    obs(s, "frame", "de1", 900, ts=t1.isoformat())
    s.db.execute("UPDATE runs SET ts_end=?, sources_total=1, sources_ok=1 WHERE id=?", ((t1 + dt.timedelta(minutes=1)).isoformat(), r2)); s.db.commit()
    # run 3: frame back up (de1 out of stock)
    t2 = t0 + dt.timedelta(hours=2)
    r3 = s.start_run(p.id)
    obs(s, "frame", "de1", 900, in_stock=0, ts=t2.isoformat())
    s.db.execute("UPDATE runs SET ts_end=?, sources_total=1, sources_ok=1 WHERE id=?", ((t2 + dt.timedelta(minutes=1)).isoformat(), r3)); s.db.commit()

    assert backfill(s, p) == 3
    assert backfill(s, p) == 0  # idempotent
    h = series(s, p)
    assert [round(x["total"]) for x in h["points"]] == [1400, 1300, 1400]
    assert h["alltime"]["total"] == 1300 and h["alltime"]["ts"].startswith("2026-11-05T09:01")
    assert len(h["changes"]) == 2 and h["changes"][0]["delta"] == 100 and h["changes"][1]["delta"] == -100
    assert any(d.startswith("Frame switched from") for d in h["changes"][1]["drivers"])
