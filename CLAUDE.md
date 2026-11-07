# ha-price-tracker — project notes for Claude

Owner: Daniel (Basel, CH). Self-hosted on his HexOS/TrueNAS box (Docker), publishes to his
Home Assistant via the Mosquitto broker app. Notifications go to two phones via the blueprint.

## What this is
Product-agnostic price tracker. `products/<id>.yaml` fully describes a product as *components*
(each with multiple seller *sources*) plus optional *bundles* (seller SKUs covering several
components). Every run: fetch due sources → parse price/stock → convert to landed cost in the
reporting currency → SQLite → aggregate cheapest complete set → publish to HA via MQTT discovery
→ emit events (new_low / price_drop / back_in_stock / tracker_error).

First product: `epriam-2026` (Cybex e-Priam 2026 New Generation: frame, seat pack, Fold Lux carry
cot, Cloud T i-Size, Base T; Lite Cot optional, no 2026 listings found). Any colour, cheapest wins.

## Decisions (do not re-raise)
- Runtime: Docker on the NAS, not a Claude scheduled task. Scheduler is in-process (no cron dep).
- Schedule: hourly 07:00–23:00 plus 00:00 Europe/Zurich (`schedule.hours: "7-23,0"`), per-source
  `interval_hours` to go easier on rate-limited shops. Failing sources back off automatically.
- Landed-cost model: EU sellers via the German parcel box (route `collect`): sticker, minus local
  VAT only when `vat_refund: yes`, plus shipping, × (1 + 8.1 % CH import VAT), no duty, no clearance
  fee. Ranking uses the conservative figure unless refund is confirmed. Non-DE EU sellers: no refund.
  Route `ddp` (e.g. Kindermaxx's CHF storefront via MeinEinkauf.ch): price already includes Swiss VAT
  and customs → landed = sticker + shipping. A source can pin a route with `route: <key>`.
- Shipping is per order: `_cheapest_set` counts it once per (seller, session) and shows an
  `adjustment` item. Observations store `shipping` (reporting currency) for that.
- Sources can carry a `prelude` request (e.g. Shopware currency switch POST) and a `session` name;
  each session has its own cookie jar per run, so EUR and CHF storefronts of one shop coexist.
- Out-of-stock offers never count as "lowest" (`alerts.require_in_stock: true`).
- Observations older than `stale_after_hours` (36) are ignored; price-drop threshold 5 %.
- HA integration via MQTT discovery only (no HA REST/API from the container). One HA device per
  product. Entity ids are forced via `default_entity_id` = `<domain>.<product>_<suffix>` (HA ≥ 2025.10; `object_id` is ignored) (see `tracker/mqtt.py`).
- Alerts via an MQTT **event** entity + the blueprint in `ha/blueprints/`. Notify services are a
  text list because mobile_app exposes no notify entities.
- Secrets only in `.env` on the NAS (never in the repo, never seen by Claude).
- No AI tooling touches Daniel's employer tenant; this is purely personal infrastructure.

## Layout
- `tracker/config.py` pydantic schema for product files + env settings
- `tracker/fetch.py` httpx with per-host pacing, retries, bot-wall detection; optional Playwright
- `tracker/parse.py` JSON-LD → meta tags → CSS/regex; localized price parsing; offer selection
- `tracker/landed.py` landed-cost model · `tracker/fx.py` ECB rates (frankfurter) cached daily
- `tracker/store.py` SQLite · `tracker/compute.py` lows, cheapest set (bundles × components), events
- `tracker/mqtt.py` HA discovery + state · `tracker/runner.py` one run · `tracker/scheduler.py`
- `tracker/history.py` one `snapshots` row per run (cheapest set + items); `backfill` replays old runs via `build_snapshot(as_of=…)`; change log + all-time low
- `tracker/web.py` stdlib status page on WEB_PORT (8099): overview, history chart + change log, per-shop tables, landed-cost rules, coverage notes, /api/<id>.json, /api/<id>/history.json
- `tracker/__main__.py` CLI: `run | schedule | validate | discovery | show | fetch | web | backfill`
- `products/` product definitions · `ha/` blueprint + dashboard card · `reports/` validation output
- `.github/workflows/` tests, source validation (runs from GitHub's network), image build

## Working conventions
- `python -m pytest -q` must pass offline (no network in tests; fixtures are inline HTML).
- Validate sources with `price-tracker validate products/<id>.yaml --report reports/<id>.md`.
- Debug a shop page with `price-tracker fetch URL --country DE`.
- Adding a source: prefer `parser: jsonld` (default; falls back to meta tags). Add `match.name_regex`
  or `match.sku_regex` when a page lists several products/variants. Set `bounds` per component so a
  mis-parse (wrong product, accessory price) is rejected rather than reported as a low.
- Sites known to bot-wall plain HTTP (2026-11): galaxus.ch, toppreise.ch, idealo.de, geizhals.de
  (403); baby-walz.de and babymarkt.de (429). Try `parser: playwright` with `WITH_PLAYWRIGHT=true`.
- Commit messages: conventional, imperative; push to `main` is allowed.
- Time-box long work; leave a status in the README "Status" section or the PR rather than running on.
