"""Status page: what is tracked, where prices come from, what each shop says now.

Stdlib only (``http.server`` in a daemon thread). Read-only against the SQLite
store; every request opens its own connection so it never contends with the
scheduler thread.

  GET /                 overview of every product
  GET /p/<product id>   one product
  GET /api/<id>.json    snapshot + health as JSON
  GET /api/<id>/history.json?days=30|all   cheapest-set history, change log, all-time low
  GET /healthz          200 while the last run is younger than 10 h
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from . import __version__
from .compute import build_snapshot, health_for
from .config import Product, Settings
from .scheduler import next_fire
from .store import Store
from .history import series as history_series

log = logging.getLogger(__name__)

FLAGS = {"CH": "🇨🇭", "DE": "🇩🇪", "FR": "🇫🇷", "AT": "🇦🇹", "IT": "🇮🇹", "NL": "🇳🇱", "GB": "🇬🇧", "LT": "🇱🇹", "PL": "🇵🇱", "ES": "🇪🇸"}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#6a737d;--line:#e3e6ea;--ok:#1a7f37;--warn:#b35c00;--bad:#c62828;--accent:#0b5fff;--chip:#eef2f7}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#161a21;--fg:#e6e8eb;--muted:#9aa4b2;--line:#262c36;--ok:#3fb950;--warn:#e3a008;--bad:#f85149;--accent:#58a6ff;--chip:#1f2630}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1200px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}h3{font-size:15px;margin:0 0 6px}
.sub{color:var(--muted);margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.big{font-size:30px;font-weight:650;letter-spacing:-.01em;margin:2px 0 6px}.big small{font-size:14px;font-weight:400;color:var(--muted);margin-left:6px}
.kv{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--muted);font-size:13px}.kv b{color:var(--fg);font-weight:600}
.items{margin:8px 0 0;padding:0;list-style:none}.items li{padding:4px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.items li:first-child{border-top:0}.items .m{color:var(--muted);font-size:12px}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{width:100%;min-width:760px;border-collapse:collapse;background:var(--card)}
th,td{padding:8px 10px;text-align:left;vertical-align:top;border-top:1px solid var(--line);font-size:13px}th{background:var(--chip);font-weight:600;color:var(--muted);border-top:0;white-space:nowrap}
td.num,th.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
tr.best td{background:color-mix(in srgb,var(--ok) 8%,transparent)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
.ok{background:var(--ok)}.warn{background:var(--warn)}.bad{background:var(--bad)}.off{background:var(--muted)}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 8px;font-size:12px;color:var(--muted);margin-right:4px;white-space:nowrap}
.err{color:var(--bad);font-size:12px;display:block;max-width:420px;overflow-wrap:anywhere}
.muted{color:var(--muted)}.small{font-size:12px}.spark{vertical-align:middle}
.notes{margin:0;padding-left:18px;color:var(--muted);font-size:13px}.notes li{margin:3px 0}
footer{margin-top:36px;color:var(--muted);font-size:12px}
details summary{cursor:pointer;color:var(--muted)}
.chart{width:100%;height:auto;display:block}
.legend{display:flex;gap:14px;color:var(--muted);font-size:12px;margin:6px 0 0}.legend i{display:inline-block;width:18px;height:0;border-top:2px solid var(--accent);vertical-align:middle;margin-right:6px}.legend i.h{border-top:2px dashed var(--muted)}
.range a{margin-right:10px}.range a.on{font-weight:600;text-decoration:underline}
.atl{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline}.atl .big{margin:0}
.delta-up{color:var(--bad)}.delta-down{color:var(--ok)}
.tag{font-size:11px;padding:1px 6px;border-radius:4px;background:var(--chip);color:var(--muted);margin-left:6px;vertical-align:middle}
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _money(v, cur="") -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}".replace(",", " ") + (f" {cur}" if cur else "")


def _ago(ts: str | None, now: dt.datetime) -> str:
    if not ts:
        return "never"
    try:
        t = dt.datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    s = int((now - t).total_seconds())
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 48 * 3600:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def _local(ts: str | None, tz: ZoneInfo) -> str:
    if not ts:
        return "—"
    try:
        t = dt.datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _hours(hours: list[int]) -> str:
    """[0,7,8,...,23] -> '07–23, 00'."""
    hs = sorted(hours)
    ranges: list[list[int]] = []
    for h in hs:
        if ranges and h == ranges[-1][1] + 1:
            ranges[-1][1] = h
        else:
            ranges.append([h, h])
    parts = [f"{a:02d}" if a == b else f"{a:02d}–{b:02d}" for a, b in ranges]
    if parts and parts[0] == "00":  # show midnight last, it belongs to the previous day's run
        parts = parts[1:] + ["00"]
    return ", ".join(parts)


def _spark(points: list[tuple[str, float]], w=110, h=22) -> str:
    if len(points) < 2:
        return ""
    xs = [dt.datetime.fromisoformat(p[0]).timestamp() for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 == x0:
        return ""
    pad = (y1 - y0) * 0.1 or 1
    y0 -= pad
    y1 += pad
    pts = " ".join(f"{(x - x0) / (x1 - x0) * (w - 2) + 1:.1f},{h - 1 - (y - y0) / (y1 - y0) * (h - 2):.1f}" for x, y in zip(xs, ys))
    color = "var(--ok)" if ys[-1] < ys[0] else ("var(--bad)" if ys[-1] > ys[0] else "var(--muted)")
    title = f"{min(ys):.0f} – {max(ys):.0f} over {len(points)} fetches"
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}"><title>{_esc(title)}</title>'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{pts}"/></svg>')


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    import math
    span = max(hi - lo, 1.0)
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    t = start
    while t <= hi + step * 0.5:
        ticks.append(round(t, 6))
        t += step
    return ticks


def _chart(points: list[dict], atl: dict | None, tz: ZoneInfo, cur: str, w: int = 900, h: int = 280) -> str:
    """Step chart of the cheapest set (solid) and home-only set (dashed), all-time low marked."""
    ml, mr, mt, mb = 64, 16, 14, 36
    pw, ph = w - ml - mr, h - mt - mb
    xs = [dt.datetime.fromisoformat(p["ts"]).timestamp() for p in points]
    ys = [p["total"] for p in points if p["total"] is not None] + [p["home"] for p in points if p["home"] is not None]
    if atl:
        ys.append(atl["total"])
    x0, x1 = min(xs), max(xs)
    if x1 == x0:
        x1 = x0 + 3600
    lo, hi = min(ys), max(ys)
    pad = max((hi - lo) * 0.08, 10)
    lo, hi = lo - pad, hi + pad
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def X(t): return ml + (t - x0) / (x1 - x0) * pw
    def Y(v): return mt + (hi - v) / (hi - lo) * ph

    def path(key: str) -> str:
        d, prev = [], None
        for p in points:
            v = p[key]
            if v is None:
                prev = None
                continue
            x, y = X(dt.datetime.fromisoformat(p["ts"]).timestamp()), Y(v)
            if prev is None:
                d.append(f"M{x:.1f},{y:.1f}")
            else:
                d.append(f"H{x:.1f}V{y:.1f}")  # step-after: price holds until the next observation
            prev = (x, y)
        if prev is not None:
            d.append(f"H{ml + pw:.1f}")
        return " ".join(d)

    g = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="price history">']
    for t in ticks:
        if lo <= t <= hi:
            y = Y(t)
            g.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)" stroke-width="1"/>'
                     f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="var(--muted)">{t:,.0f}</text>')
    # x labels: ~6 evenly spaced
    span = x1 - x0
    nlab = 6
    for i in range(nlab + 1):
        t = x0 + span * i / nlab
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(tz)
        lbl = d.strftime("%d.%m %H:%M") if span < 3 * 86400 else d.strftime("%d.%m")
        anchor = "start" if i == 0 else ("end" if i == nlab else "middle")
        g.append(f'<text x="{X(t):.1f}" y="{h - 12}" text-anchor="{anchor}" font-size="11" fill="var(--muted)">{lbl}</text>')
    g.append(f'<path d="{path("home")}" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="5 4"/>')
    g.append(f'<path d="{path("total")}" fill="none" stroke="var(--accent)" stroke-width="2.2"/>')
    # all-time low marker (only if inside the plotted range)
    if atl:
        ta = dt.datetime.fromisoformat(atl["ts"]).timestamp()
        if x0 <= ta <= x1:
            x, y = X(ta), Y(atl["total"])
            label = f'{atl["total"]:,.0f} {cur} · {dt.datetime.fromisoformat(atl["ts"]).astimezone(tz).strftime("%d.%m %H:%M")}'
            tx = x + 10 if x < ml + pw * 0.7 else x - 10
            g.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="var(--ok)" stroke="var(--card)" stroke-width="2"/>'
                     f'<text x="{tx:.1f}" y="{y - 10:.1f}" text-anchor="{"start" if tx > x else "end"}" font-size="12" font-weight="600" fill="var(--ok)">low {label}</text>')
    # current value label
    last = next((p for p in reversed(points) if p["total"] is not None), None)
    if last:
        y = Y(last["total"])
        g.append(f'<text x="{ml + pw:.1f}" y="{y - 8:.1f}" text-anchor="end" font-size="12" font-weight="600" fill="var(--accent)">now {last["total"]:,.0f}</text>')
    # hover points (titles)
    for p in points:
        if p["total"] is None:
            continue
        x, y = X(dt.datetime.fromisoformat(p["ts"]).timestamp()), Y(p["total"])
        when = dt.datetime.fromisoformat(p["ts"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
        g.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="transparent"><title>{when}: {p["total"]:,.2f} {cur}'
                 + (f' · home {p["home"]:,.2f}' if p["home"] is not None else "") + '</title></circle>')
    g.append("</svg>")
    return "".join(g)


class Renderer:
    def __init__(self, settings: Settings, products: list[Product]):
        self.settings = settings
        self.products = products

    # ------------------------------------------------------------------ data
    def _snapshot(self, store: Store, p: Product):
        fx = {}
        for _g, _b, _grp, s in p.all_sources():
            cur = s.currency or ("CHF" if s.country == "CH" else "EUR")
            if cur != p.imports.reporting_currency and cur not in fx:
                fx[cur] = store.latest_fx(cur, p.imports.reporting_currency) or 0.0
        snap = build_snapshot(p, store, fx, {})
        snap.health = health_for(p, snap)
        return snap

    def api(self, store: Store, pid: str) -> dict | None:
        p = next((x for x in self.products if x.id == pid), None)
        if not p:
            return None
        snap = self._snapshot(store, p)
        return {"product": p.id, "name": p.name, "ts": snap.ts, "total": snap.total, "components": snap.components,
                "bundles": snap.bundles, "alltime": snap.alltime, "health": snap.health, "fx": snap.fx}

    # ------------------------------------------------------------------ history
    def _history_section(self, store: Store, p: Product, days: int | None, tz: ZoneInfo, cur: str) -> str:
        hist_all = history_series(store, p)            # all-time low must consider everything
        hist = history_series(store, p, days) if days else hist_all
        atl, now_pt = hist_all["alltime"], hist_all["current"]
        out = ['<h2 id="history">Cheapest complete set over time</h2>']
        # range selector
        opts = [(7, "7 d"), (30, "30 d"), (90, "90 d"), (None, "all")]
        out.append('<p class="sub range">' + " ".join(
            f'<a class="{"on" if d == days else ""}" href="?days={d if d else "all"}#history">{lbl}</a>' for d, lbl in opts) + "</p>")
        # all-time low card
        if atl:
            cur_total = now_pt["total"] if now_pt else None
            delta = (cur_total - atl["total"]) if cur_total is not None else None
            dtxt = ("" if delta is None else
                    (f'<span class="delta-up">+{_money(delta)} above the low</span>' if delta > 0.005 else '<span class="delta-down">at the all-time low</span>'))
            parts = ", ".join(f'{_esc(i.get("seller"))} ({_esc(i.get("id"))})' for i in atl["items"] if not i.get("missing") and i.get("kind") != "adjustment")
            out.append(f'<div class="card"><div class="atl"><span class="muted">All-time low</span><div class="big">{_money(atl["total"])}<small>{cur}</small></div>'
                       f'<span class="muted">on {_esc(_local(atl["ts"], tz))}</span><span>{dtxt}</span></div>'
                       f'<div class="small muted" style="margin-top:4px">{parts}</div></div>')
        pts = [x for x in hist["points"] if x["total"] is not None]
        if len(pts) >= 2:
            out.append('<div class="card" style="margin-top:12px">' + _chart(hist["points"], atl, tz, cur) +
                       '<div class="legend"><span><i></i>cheapest complete set</span><span><i class="h"></i>home-country sellers only</span>'
                       '<span>● all-time low</span></div></div>')
        elif pts:
            out.append('<p class="sub">Only one data point so far — the chart appears after the next run.</p>')
        else:
            out.append('<p class="sub">No history yet.</p>')
        # change log
        ch = hist["changes"]
        if ch:
            rows = "".join(
                f'<tr><td class="small muted">{_esc(_local(c["ts"], tz))}</td><td class="num">{_money(c["from"])}</td>'
                f'<td class="num"><b>{_money(c["to"])}</b></td>'
                f'<td class="num {"delta-up" if c["delta"] > 0 else "delta-down"}">{"+" if c["delta"] > 0 else ""}{_money(c["delta"])}</td>'
                f'<td class="small">{"<br>".join(_esc(d) for d in c["drivers"]) or "—"}</td></tr>'
                for c in ch[:40])
            out.append('<h3 style="margin-top:14px">Changes</h3><div class="tw"><table><thead><tr><th>When</th><th class="num">From</th>'
                       '<th class="num">To</th><th class="num">Δ</th><th>What moved</th></tr></thead><tbody>' + rows + "</tbody></table></div>")
        else:
            out.append('<p class="sub small">No change in the cheapest set within this range.</p>')
        return "".join(out)

    # ------------------------------------------------------------------ html
    def page(self, store: Store, only: str | None = None, days: int | None = 30) -> str:
        prods = [p for p in self.products if only is None or p.id == only]
        body = "".join(self._product(store, p, days) for p in prods) if prods else "<p>No such product.</p>"
        title = prods[0].name if len(prods) == 1 else "Price tracker"
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{_esc(title)}</title><style>{CSS}</style></head><body><div class=\"wrap\">{body}"
                f'<footer>ha-price-tracker {__version__} · <a href="https://github.com/neocleous/price-tracker">source</a> · '
                f'<a href="/api/{_esc(prods[0].id) if prods else ""}.json">json</a> · <a href="/api/{_esc(prods[0].id) if prods else ""}/history.json?days=all">history json</a></footer></div></body></html>')

    def _product(self, store: Store, p: Product, days: int | None = 30) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        tz = ZoneInfo(p.schedule.timezone)
        cur = p.imports.reporting_currency
        snap = self._snapshot(store, p)
        h = snap.health
        last = store.last_run(p.id)
        try:
            nxt = next_fire(p.schedule).strftime("%H:%M")
        except ValueError:
            nxt = "—"
        out = [f"<h1>{_esc(p.name)}</h1>"]
        if p.description:
            out.append(f'<p class="sub">{_esc(p.description.strip())}</p>')
        status_cls = "bad" if h["problem"] else ("warn" if h["sources_ok"] < h["sources_total"] else "ok")
        out.append(
            '<div class="kv">'
            f'<span><span class="dot {status_cls}"></span><b>{h["sources_ok"]}/{h["sources_total"]}</b> sources with a fresh price</span>'
            f'<span>last run <b>{_esc(_local(last["ts_end"], tz)) if last else "—"}</b> ({_ago(last["ts_end"], now) if last else "never"})</span>'
            f'<span>next run <b>{nxt}</b></span>'
            f'<span>schedule <b>hourly {_esc(_hours(p.schedule.hours))}</b> {_esc(p.schedule.timezone)}</span>'
            + "".join(f'<span>1 {_esc(k)} = <b>{v:.4f} {cur}</b></span>' for k, v in snap.fx.items())
            + "</div>"
        )

        # ---- summary cards
        def _set_card(title: str, tot: dict, extra: str = "") -> str:
            items = tot.get("items") or []
            lis = []
            for i in items:
                if i.get("missing"):
                    lis.append(f'<li><span>{_esc(i["id"])}</span><span class="m">no fresh offer</span></li>')
                    continue
                if i.get("kind") == "adjustment":
                    lis.append(f'<li><span class="m">shipping counted once per order</span><span><b>{_money(i["landed"])}</b></span></li>')
                    continue
                kind = "bundle" if i["kind"] == "bundle" else "part"
                ref = "" if i.get("country") == p.imports.home_country else (
                    ' · VAT refund assumed' if i.get("refund_applied") else ' · no refund assumed')
                lis.append(
                    f'<li><span>{FLAGS.get(i.get("country"), "")} <a href="{_esc(i["url"])}" target="_blank" rel="noopener">{_esc(i["seller"])}</a>'
                    f' <span class="tag">{kind}: {_esc(i["id"])}</span></span>'
                    f'<span><b>{_money(i["landed"])}</b> <span class="m">({_money(i["sticker"], i["currency"])}{ref})</span></span></li>'
                )
            val = tot.get("value")
            vtxt = f'{_money(val)}<small>{cur} landed</small>' if val is not None else f'<span class="muted">incomplete</span><small>{_money(tot.get("partial_value"))} {cur} for what is available</small>'
            return f'<div class="card"><h3>{_esc(title)}</h3><div class="big">{vtxt}</div>{extra}<ul class="items">{"".join(lis)}</ul></div>'

        at = snap.alltime.get("total") or {}
        at_txt = (f'<div class="kv"><span>all-time low <b>{_money(at.get("value"))} {cur}</b> on {_esc(_local(at.get("ts"), tz))}</span></div>'
                  if at else "")
        out.append('<div class="grid">')
        out.append(_set_card("Cheapest complete set", snap.total, at_txt))
        out.append(_set_card(f"Cheapest complete set — {p.imports.home_country} sellers only", snap.total.get("home", {})))
        out.append("</div>")
        out.append(self._history_section(store, p, days, tz, cur))

        # ---- per group tables
        def _group_table(grp, view, is_bundle: bool) -> str:
            latest_any = store.latest_per_source(p.id, grp.id, ok_only=False)
            hist = store.history(p.id, grp.id, days=30)
            series: dict[str, list[tuple[str, float]]] = {}
            for r in hist:
                series.setdefault(r["source_id"], []).append((r["ts"], float(r["landed"])))
            best_sid = (view.get("lowest") or {}).get("source")
            rows = []
            if not grp.sources:
                rows.append('<tr><td colspan="9" class="muted">No sources configured yet.</td></tr>')
            for src in grp.sources:
                s = view["sources"].get(src.id)
                la = latest_any.get(src.id)
                if not src.enabled:
                    dot, state = "off", "disabled"
                elif s and s.get("fresh") and (la is None or la["ok"]):
                    dot, state = "ok", "ok"
                elif s and s.get("fresh"):
                    dot, state = "warn", "last fetch failed, showing previous price"
                elif la is not None and not la["ok"]:
                    dot, state = "bad", "failing"
                else:
                    dot, state = "off", "no data yet"
                cur_src = src.currency or ("CHF" if src.country == "CH" else "EUR")
                price = _money(s["sticker"], s["currency"]) if s else "—"
                landed = _money(s["landed"]) if s else "—"
                nor = (f'<div class="small muted">{_money(s["landed_no_refund"])} without refund</div>'
                       if s and abs(s["landed_no_refund"] - s["landed"]) > 0.01 else "")
                stock = "—" if not s or s.get("in_stock") is None else ("in stock" if s["in_stock"] else '<span class="muted">out of stock</span>')
                err = f'<span class="err">{_esc(la["error"])}</span>' if la is not None and not la["ok"] and la["error"] else ""
                vat = {"yes": "refund", "no": "no refund", "unknown": "refund unknown"}[src.vat_refund] if src.country != p.imports.home_country else "domestic"
                rows.append(
                    f'<tr class="{"best" if src.id == best_sid else ""}">'
                    f'<td><span class="dot {dot}" title="{_esc(state)}"></span>{FLAGS.get(src.country, "")} <a href="{_esc(src.url)}" target="_blank" rel="noopener">{_esc(src.seller)}</a>'
                    f'{" <span class=chip>" + _esc(src.notes) + "</span>" if src.notes else ""}{err}</td>'
                    f'<td class="num">{price}</td><td class="num"><b>{landed}</b>{nor}</td>'
                    f'<td><span class="chip">{vat}</span></td><td>{stock}</td>'
                    f'<td>{_spark(series.get(src.id, []))}</td>'
                    f'<td class="small muted">{_ago(s["fetched_at"], now) if s else (_ago(la["ts"], now) if la is not None else "never")}</td>'
                    f'<td class="small muted">{_esc(src.parser)} · every {src.interval_hours:g} h</td>'
                    f'<td class="small muted" title="{_esc(s["currency"] if s else cur_src)}">{_esc(s.get("fresh") and (s["in_stock"] is not False) and "counts" or "ignored") if s else ""}</td>'
                    "</tr>"
                )
            lo = view.get("lowest")
            head = (f'<h2 id="{_esc(grp.id)}">{_esc(grp.name)}'
                    + (f' <span class="tag">bundle: {" + ".join(_esc(c) for c in grp.covers)}</span>' if is_bundle else (
                        '' if getattr(grp, "required", True) else ' <span class="tag">optional</span>'))
                    + "</h2>")
            sub = (f'<p class="sub">Lowest now: <b>{_money(lo["landed"])} {cur}</b> at {_esc(lo["seller"])}'
                   + (f' · all-time low {_money((snap.alltime.get(grp.id) or {}).get("value"))} {cur}' if snap.alltime.get(grp.id) else "")
                   + f' · sanity range {grp.bounds.min:.0f}–{grp.bounds.max:.0f} {cur}</p>') if lo else \
                  f'<p class="sub">No fresh in-stock offer · sanity range {grp.bounds.min:.0f}–{grp.bounds.max:.0f} {cur}</p>'
            return (head + sub + '<div class="tw"><table><thead><tr><th>Shop</th><th class="num">Sticker</th><th class="num">Landed</th><th>VAT</th>'
                    '<th>Stock</th><th>30 d</th><th>Fetched</th><th>Parser</th><th>Rank</th></tr></thead><tbody>'
                    + "".join(rows) + "</tbody></table></div>")

        out.append("<h2>Components</h2>")
        for c in p.components:
            out.append(_group_table(c, snap.components[c.id], False))
        if p.bundles:
            out.append("<h2>Bundles</h2><p class=\"sub\">Seller SKUs that cover several components; the cheapest-set calculation combines them with individually bought parts.</p>")
            for b in p.bundles:
                out.append(_group_table(b, snap.bundles[b.id], True))

        # ---- model + coverage
        r = p.imports
        out.append("<h2>How landed prices are calculated</h2><div class=\"card\"><ul class=\"notes\">")
        out.append(f'<li>Reporting currency {cur}; import VAT {r.import_vat*100:.1f} % on goods + shipping; duty {r.duty*100:.0f} %.</li>')
        out.append(f'<li>{FLAGS.get(r.home_country,"")} {r.home_country} sellers: sticker + shipping. {_esc(r.home_delivery_note or "")}</li>')
        for ccode, route in r.routes.items():
            out.append(f'<li>{FLAGS.get(ccode,"")} {ccode} ({route.mode}): local VAT {route.local_vat*100:.0f} % deducted only when a shop is marked <i>vat_refund: yes</i>'
                       f'{"" if route.refund_possible else " (refund not possible on this route)"}; default shipping {route.default_shipping:g}'
                       f'{"; clearance fee " + format(route.clearance_fee, "g") + " " + cur if route.clearance_fee else ""}. {_esc(route.delivery_note or "")}</li>')
        d = r.default_route
        out.append(f'<li>Other countries ({d.mode}): local VAT {d.local_vat*100:.0f} %{"" if d.refund_possible else ", no refund"}; default shipping {d.default_shipping:g}. {_esc(d.delivery_note or "")}</li>')
        out.append(f'<li>Out-of-stock offers {"never" if p.alerts.require_in_stock else "do"} count as lowest; observations older than {p.alerts.stale_after_hours:g} h are ignored; '
                   f'a shop price drop of ≥ {p.alerts.price_drop_pct:g} % raises an alert.</li></ul></div>')
        if p.coverage_notes:
            out.append("<h2>Coverage notes</h2><div class=\"card\"><ul class=\"notes\">" + "".join(f"<li>{_esc(n)}</li>" for n in p.coverage_notes) + "</ul></div>")
        if h["without_fresh_price"]:
            out.append(f'<p class="sub small">Without a fresh price right now: {_esc(", ".join(h["without_fresh_price"]))}</p>')
        return "".join(out)


class _Handler(BaseHTTPRequestHandler):
    renderer: Renderer  # set by start_web
    db_path: str

    def log_message(self, fmt, *args):  # quiet
        log.debug("web: " + fmt, *args)

    def do_GET(self):  # noqa: N802
        path, _, qs = self.path.partition("?")
        params = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv)
        d = params.get("days", "30")
        days = None if d == "all" else (int(d) if d.isdigit() else 30)
        store = Store(self.db_path)
        try:
            if path.startswith("/api/") and path.endswith("/history.json"):
                pid = path[5:-len("/history.json")]
                prod = next((x for x in self.renderer.products if x.id == pid), None)
                if prod is None:
                    self._send(404, "not found", "text/plain")
                else:
                    self._send(200, json.dumps(history_series(store, prod, days), ensure_ascii=False, default=str), "application/json")
                return
            if path == "/healthz":
                last = max((store.last_run(p.id) for p in self.renderer.products), key=lambda r: (r["ts_end"] if r else ""), default=None)
                ok = bool(last) and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last["ts_end"])).total_seconds() < 10 * 3600
                self._send(200 if ok else 503, "ok" if ok else "stale", "text/plain")
            elif path.startswith("/api/") and path.endswith(".json"):
                data = self.renderer.api(store, path[5:-5])
                if data is None:
                    self._send(404, "not found", "text/plain")
                else:
                    self._send(200, json.dumps(data, ensure_ascii=False, default=str), "application/json")
            elif path == "/" or path == "/index.html":
                self._send(200, self.renderer.page(store, days=days), "text/html")
            elif path.startswith("/p/"):
                self._send(200, self.renderer.page(store, only=path[3:].strip("/"), days=days), "text/html")
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            log.exception("web request failed")
            self._send(500, f"error: {e}", "text/plain")
        finally:
            store.close()

    def _send(self, code: int, body: str, ctype: str):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def start_web(settings: Settings, products: list[Product], port: int | None = None) -> ThreadingHTTPServer | None:
    port = settings.web_port if port is None else port
    if not port:
        return None
    handler = type("Handler", (_Handler,), {"renderer": Renderer(settings, products), "db_path": str(settings.db_path)})
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="web", daemon=True).start()
    log.info("status page on http://0.0.0.0:%d/", port)
    return srv
