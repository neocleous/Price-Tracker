"""Top-of-the-hour scheduler restricted to the product's allowed hours, in its
timezone. No external dependency; survives DST changes via zoneinfo."""

from __future__ import annotations

import datetime as dt
import logging
import signal
import time
from zoneinfo import ZoneInfo

from .config import Schedule

log = logging.getLogger(__name__)


def next_fire(schedule: Schedule, now: dt.datetime | None = None) -> dt.datetime:
    tz = ZoneInfo(schedule.timezone)
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    candidate = now.replace(minute=schedule.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(hours=1)
    for _ in range(48):
        if candidate.hour in schedule.hours:
            return candidate
        candidate += dt.timedelta(hours=1)
    raise ValueError("schedule has no allowed hours")


class Stop(Exception):
    pass


def run_forever(schedules: list[Schedule], tick):
    """Call ``tick()`` at every fire time of any schedule. ``tick`` receives the
    fire time and must not raise."""
    stop = {"flag": False}

    def _sig(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    while not stop["flag"]:
        nxt = min(next_fire(s) for s in schedules)
        wait = (nxt - dt.datetime.now(dt.timezone.utc)).total_seconds()
        log.info("next run at %s (in %.0f min)", nxt.isoformat(), wait / 60)
        while wait > 0 and not stop["flag"]:
            time.sleep(min(wait, 30))
            wait = (nxt - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if stop["flag"]:
            break
        try:
            tick(nxt)
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
    log.info("scheduler stopped")
