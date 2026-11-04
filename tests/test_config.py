from pathlib import Path

import pytest

from tracker.config import Schedule, load_product, load_products
from tracker.scheduler import next_fire
import datetime as dt
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def test_all_shipped_products_validate():
    prods = load_products(ROOT / "products")
    assert prods, "no product files"
    for p in prods:
        assert p.components
        assert any(c.sources for c in p.components)


def test_schedule_hours_string():
    s = Schedule(hours="7-23,0")
    assert s.hours == [0, *range(7, 24)]


def test_next_fire_respects_window():
    s = Schedule(timezone="Europe/Zurich", hours="7-23,0", minute=0)
    tz = ZoneInfo("Europe/Zurich")
    at = dt.datetime(2026, 11, 5, 1, 30, tzinfo=tz)  # 01:30 → next is 07:00
    assert next_fire(s, at) == dt.datetime(2026, 11, 5, 7, 0, tzinfo=tz)
    at = dt.datetime(2026, 11, 5, 23, 10, tzinfo=tz)  # 23:10 → 00:00
    assert next_fire(s, at) == dt.datetime(2026, 11, 6, 0, 0, tzinfo=tz)
    at = dt.datetime(2026, 11, 5, 9, 0, tzinfo=tz)  # exactly on the hour → next hour
    assert next_fire(s, at) == dt.datetime(2026, 11, 5, 10, 0, tzinfo=tz)
