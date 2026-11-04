"""CLI.

  price-tracker run        [--product ID] [--all] [--only SRC,...] [--no-publish]
  price-tracker schedule                       # foreground loop, honours product schedules
  price-tracker validate   PRODUCT.yaml [--report out.md] [--json out.json]
  price-tracker discovery  [--product ID]      # (re)publish HA discovery only
  price-tracker show       [--product ID]      # print latest snapshot as JSON
  price-tracker fetch      URL [--country CC]  # debug: dump offers found on a page
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

from .compute import build_snapshot
from .config import Product, Settings, load_product, load_products
from .mqtt import Publisher
from .runner import Runner
from .scheduler import run_forever
from .store import Store


def _setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _products(settings: Settings, product_id: str | None, path: str | None) -> list[Product]:
    if path:
        return [load_product(path)]
    prods = load_products(settings.products_dir)
    if product_id:
        prods = [p for p in prods if p.id == product_id]
    if not prods:
        sys.exit(f"no products found in {settings.products_dir}" + (f" with id {product_id}" if product_id else ""))
    return prods


def cmd_run(args, settings: Settings):
    store = Store(settings.db_path)
    pub = None if args.no_publish else Publisher(settings, store)
    runner = Runner(settings, store, pub)
    try:
        for p in _products(settings, args.product, args.file):
            if pub:
                pub.publish_discovery(p)
            snap = runner.run_product(p, force_all=args.all, only=set(args.only.split(",")) if args.only else None,
                                      publish=not args.no_publish)
            print(json.dumps({"product": p.id, "total": snap.total, "events": snap.events, "health": snap.health},
                             indent=2, ensure_ascii=False, default=str))
    finally:
        if pub:
            pub.close()
        store.close()


def cmd_schedule(args, settings: Settings):
    store = Store(settings.db_path)
    pub = Publisher(settings, store)
    runner = Runner(settings, store, pub)
    prods = _products(settings, args.product, None)
    # keep retrying the broker instead of crash-looping the container
    delay = 10
    while True:
        try:
            pub.connect()
            break
        except ConnectionError as e:
            logging.getLogger(__name__).error("%s — retrying in %d s", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 300)
    for p in prods:
        pub.publish_discovery(p)
    if args.run_now:
        for p in prods:
            _safe_run(runner, pub, p)

    def tick(fire_time: dt.datetime):
        for p in prods:
            if fire_time.astimezone(dt.timezone.utc).hour in _utc_hours(p):
                _safe_run(runner, pub, p)

    try:
        run_forever([p.schedule for p in prods], tick)
    finally:
        pub.close()
        store.close()


def _utc_hours(p: Product) -> set[int]:
    """Allowed hours of *this* product translated to UTC for today (DST-safe enough for hourly ticks)."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(p.schedule.timezone)
    today = dt.datetime.now(tz).date()
    out = set()
    for h in p.schedule.hours:
        local = dt.datetime.combine(today, dt.time(h, p.schedule.minute), tzinfo=tz)
        out.add(local.astimezone(dt.timezone.utc).hour)
    return out


def _safe_run(runner: Runner, pub: Publisher, p: Product):
    try:
        runner.run_product(p)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).exception("run failed for %s", p.id)
        try:
            pub.publish_error(p, f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass


def cmd_validate(args, settings: Settings):
    """Fetch every source once (ignoring intervals), never publish, write a report."""
    product = load_product(args.file)
    store = Store(args.db or ":memory:")
    runner = Runner(settings, store, None)
    snap = runner.run_product(product, force_all=True, publish=False, workers=args.workers)
    rows = []
    for grp in (*product.components, *product.bundles):
        latest = store.latest_per_source(product.id, grp.id, ok_only=False)
        for src in grp.sources:
            r = latest.get(src.id)
            rows.append({
                "group": grp.id, "source": src.id, "seller": src.seller, "country": src.country, "parser": src.parser,
                "enabled": src.enabled,
                "ok": bool(r and r["ok"]), "sticker": r["sticker"] if r else None, "currency": r["currency"] if r else None,
                "landed": round(r["landed"], 2) if r and r["landed"] else None,
                "in_stock": r["in_stock"] if r else None, "method": r["method"] if r else None,
                "title": (r["title"] or "")[:80] if r else "", "error": (r["error"] or "") if r else "not fetched",
                "url": src.url,
            })
    ok = sum(1 for r in rows if r["ok"])
    print(f"\n{ok}/{len(rows)} sources OK")
    for r in rows:
        flag = "OK " if r["ok"] else "ERR"
        detail = f"{r['sticker']} {r['currency']} → {r['landed']} {product.imports.reporting_currency} stock={r['in_stock']} [{r['method']}] {r['title']}" if r["ok"] else r["error"]
        print(f"  {flag} {r['group']}/{r['source']:<22} {detail}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({"ts": snap.ts, "product": product.id, "ok": ok, "total": len(rows),
                                               "sources": rows, "snapshot_total": snap.total}, indent=2,
                                              ensure_ascii=False, default=str))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(_markdown_report(product, rows, snap))
    store.close()
    return 0 if ok else 1


def _markdown_report(product: Product, rows: list[dict], snap) -> str:
    cur = product.imports.reporting_currency
    ok = sum(1 for r in rows if r["ok"])
    out = [f"# Source validation — {product.name}", "", f"Run: {snap.ts}  ", f"Sources OK: **{ok}/{len(rows)}**  ",
           f"Cheapest complete set: **{snap.total.get('value')} {cur}** (complete={snap.total.get('complete')})", "",
           "| Group | Source | Country | Parser | Status | Sticker | Landed | Stock | Method | Detail |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        status = "✅" if r["ok"] else ("⏸️" if not r["enabled"] else "❌")
        sticker = f"{r['sticker']:.2f} {r['currency']}" if r["sticker"] else ""
        landed = f"{r['landed']:.0f}" if r["landed"] else ""
        detail = (r["title"] if r["ok"] else r["error"]).replace("|", "/")[:120]
        out.append(f"| {r['group']} | [{r['source']}]({r['url']}) | {r['country']} | {r['parser']} | {status} | {sticker} | {landed} | {r['in_stock']} | {r['method'] or ''} | {detail} |")
    out += ["", "## Cheapest set breakdown", "", "```json", json.dumps(snap.total, indent=2, ensure_ascii=False, default=str), "```", ""]
    return "\n".join(out)


def cmd_discovery(args, settings: Settings):
    store = Store(settings.db_path)
    pub = Publisher(settings, store)
    try:
        for p in _products(settings, args.product, None):
            pub.publish_discovery(p)
    finally:
        pub.close()
        store.close()


def cmd_show(args, settings: Settings):
    store = Store(settings.db_path)
    for p in _products(settings, args.product, None):
        snap = build_snapshot(p, store, {}, {})
        print(json.dumps({"components": snap.components, "bundles": snap.bundles, "total": snap.total,
                          "alltime": snap.alltime}, indent=2, ensure_ascii=False, default=str))
    store.close()


def cmd_fetch(args, settings: Settings):
    from .config import Match, Source
    from .fetch import Fetcher
    from .parse import offers_from_jsonld, offers_from_meta, page_title
    from bs4 import BeautifulSoup

    src = Source(id="debug", seller="debug", url=args.url, country=args.country, parser="playwright" if args.playwright else "jsonld")
    f = Fetcher(settings)
    try:
        html = f.get(src)
    finally:
        f.close()
    soup = BeautifulSoup(html, "lxml")
    print("title:", page_title(html))
    print("length:", len(html), "ld+json blocks:", html.count("ld+json"))
    offers = offers_from_jsonld(soup) or offers_from_meta(soup, None)
    for o in offers:
        print(f"  {o.method:<9} {o.price:>10.2f} {o.currency or '?':<4} stock={o.in_stock} sku={o.sku} name={o.name!r}")
    if not offers:
        print("  no structured offers found")
        if args.dump:
            Path(args.dump).write_text(html)
            print("  html written to", args.dump)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="price-tracker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run"); r.add_argument("--product"); r.add_argument("--file"); r.add_argument("--all", action="store_true")
    r.add_argument("--only"); r.add_argument("--no-publish", action="store_true"); r.set_defaults(fn=cmd_run)

    s = sub.add_parser("schedule"); s.add_argument("--product"); s.add_argument("--run-now", action="store_true", default=True)
    s.add_argument("--no-run-now", dest="run_now", action="store_false"); s.set_defaults(fn=cmd_schedule)

    v = sub.add_parser("validate"); v.add_argument("file"); v.add_argument("--report"); v.add_argument("--json")
    v.add_argument("--db"); v.add_argument("--workers", type=int, default=4); v.set_defaults(fn=cmd_validate)

    d = sub.add_parser("discovery"); d.add_argument("--product"); d.set_defaults(fn=cmd_discovery)
    sh = sub.add_parser("show"); sh.add_argument("--product"); sh.set_defaults(fn=cmd_show)

    fe = sub.add_parser("fetch"); fe.add_argument("url"); fe.add_argument("--country", default="DE")
    fe.add_argument("--playwright", action="store_true"); fe.add_argument("--dump"); fe.set_defaults(fn=cmd_fetch)

    args = ap.parse_args(argv)
    settings = Settings.from_env()
    _setup_logging(settings.log_level)
    rc = args.fn(args, settings)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
