# ha-price-tracker

Self-hosted, product-agnostic price tracker that finds the **cheapest way to buy a complete
set of components** across Swiss and EU shops, normalises everything to a **landed price in CHF**
(shipping, VAT refund, Swiss import VAT), and publishes the result to **Home Assistant via MQTT
discovery** — with push alerts for new lows, price drops and restocks.

First product: the Cybex e-Priam 2026 travel system (`products/epriam-2026.yaml`).

```
shops ──httpx/JSON-LD──▶ tracker (Docker, hourly) ──MQTT discovery──▶ Home Assistant ──blueprint──▶ phones
                              │
                           SQLite history
```

## What you get in Home Assistant

One device per product (e.g. *Cybex e-Priam 2026*) with:

| Entity | Meaning |
|---|---|
| `sensor.<id>_total_lowest` | Cheapest complete set, landed CHF. Attributes list each item (seller, URL, bundle/component, VAT assumption). |
| `sensor.<id>_total_lowest_home` | Same, Swiss sellers only. |
| `sensor.<id>_alltime_low_total` | Lowest complete-set price ever observed. |
| `sensor.<id>_<component>_lowest` / `_lowest_home` | Per component, with seller/URL/sticker attributes. |
| `sensor.<id>_<component>_<source>` + `binary_sensor…_in_stock` | Every tracked offer. |
| `event.<id>_alerts` | `new_low`, `price_drop`, `back_in_stock`, `tracker_error` — drives the blueprint. |
| `sensor.<id>_last_run`, `sensor.<id>_sources_ok`, `binary_sensor.<id>_problem`, `sensor.<id>_fx_eur` | Diagnostics. |

`ha/dashboard-card.yaml` is a ready-made card; `ha/blueprints/price_tracker_alerts.yaml` the alert automation.

## Status page

The container serves a read-only overview on port **8099** (`http://<nas-ip>:8099/`): cheapest complete set with
links, the Swiss-only alternative, every component and bundle with each shop's sticker and landed price, VAT
assumption, stock, 30-day sparkline, last fetch and last error, the landed-cost rules, and coverage notes.
`/api/<product>.json` returns the same as JSON; `/healthz` is 200 while the last run is under 10 h old.
Set `WEB_PORT=0` to disable. `price-tracker web --port 8099` serves it standalone (e.g. against a copied database).

## Install (HexOS / TrueNAS SCALE, Docker Compose)

1. **Mosquitto user** — in the Mosquitto app configuration add a login, e.g.
   `price-tracker` / `<password>`. The MQTT integration in HA must already be set up (discovery on).
2. **Clone & configure** on the NAS (SSH or a shell app):
   ```bash
   git clone https://github.com/neocleous/price-tracker.git /mnt/<pool>/apps/price-tracker
   cd /mnt/<pool>/apps/price-tracker
   cp .env.example .env && nano .env      # MQTT_HOST = HA IP, MQTT_USER/PASSWORD
   mkdir -p data
   ```
3. **Dry run** (no MQTT, no publish) to see every source parsed:
   ```bash
   docker compose run --rm price-tracker validate /config/products/epriam-2026.yaml
   ```
4. **Start**:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   On start it publishes discovery, runs once immediately, then at every allowed hour.
   HexOS/TrueNAS: alternatively paste `docker-compose.yml` into *Apps → Discover → Custom App → Install via YAML*
   with the two volume paths adjusted; the `.env` values then go into the YAML `environment:` block.
5. **Home Assistant**
   - Settings → Devices & services → MQTT → the new device appears within a minute.
   - Import the blueprint: Settings → Automations → Blueprints → Import,
     `https://github.com/neocleous/price-tracker/blob/main/ha/blueprints/price_tracker_alerts.yaml`
     (or let Claude import it through the HA connector). Create an automation from it: pick
     `event.<id>_alerts`, enter the notify service names, optionally a target price.
   - Add `ha/dashboard-card.yaml` to a dashboard.

Need the bot-walled shops (Galaxus, toppreise, idealo, geizhals)? Build with Chromium:
`WITH_PLAYWRIGHT: "true"` in `docker-compose.yml`, raise `mem_limit`, set `parser: playwright` on those sources.

## Define a product

```yaml
id: my-thing
name: My Thing
imports:                      # landed-cost rules for the buyer
  home_country: CH
  reporting_currency: CHF
  import_vat: 0.081
  routes:
    DE: {mode: collect, local_vat: 0.19, default_shipping: 0, refund_possible: true}
  default_route: {mode: collect, local_vat: 0.20, default_shipping: 25, refund_possible: false}
alerts: {price_drop_pct: 5, require_in_stock: true, stale_after_hours: 36}
schedule: {timezone: Europe/Zurich, hours: "7-23,0", minute: 0}
components:
  - id: widget
    name: Widget
    bounds: {min: 100, max: 500}          # landed CHF sanity range; outside = parse error
    sources:
      - id: shop_ch
        seller: Shop CH
        url: https://shop.ch/widget
        country: CH                        # picks the route and the default currency
        parser: jsonld                     # jsonld (default) | meta | css | regex | playwright
        vat_refund: unknown                # yes | no | unknown (unknown ranks as "no")
        interval_hours: 1
        match: {name_regex: "Widget", sku_regex: "^W-"}   # choose among variants on the page
bundles:                                   # optional: one SKU covering several components
  - id: widget_plus_gadget
    name: Widget + Gadget set
    covers: [widget, gadget]
    bounds: {min: 150, max: 900}
    sources: [...]
```

Routes: `domestic` (home country), `collect` (you pick the parcel up in that country and import it
yourself — no carrier fee; local VAT deducted only when `vat_refund: yes` and `refund_possible`),
`ship` (sent to the home country; `clearance_fee` added), `ddp` (the shop's price already includes
your VAT and customs — e.g. a foreign shop's Swiss storefront; landed = sticker + shipping). A source
pins a non-default route with `route: <key>`. Shipping is counted once per seller and order in the
cheapest-set calculation. Duty is 0 for CH since 2024.

Shops whose price depends on a session setting (currency/country switch) take a `prelude` request and
a `session` name; sources sharing a session share one cookie jar per run:

```yaml
      - id: shop_ch
        seller: Shop → CH (DDP)
        url: https://shop.de/product
        country: DE
        route: shop_ddp
        session: shop_ch
        currency: CHF
        prelude: {method: POST, url: "https://shop.de/en/checkout/configure", data: {currencyId: "<id>"}}
```

Landed-cost outputs per offer: `landed` (refund applied only when confirmed) and
`landed_no_refund` (conservative) — both in the entity attributes.

## CLI

```
price-tracker run        [--product ID] [--all] [--only SRC,...] [--no-publish]
price-tracker schedule   [--product ID] [--no-run-now]
price-tracker validate   products/x.yaml [--report out.md] [--json out.json]
price-tracker discovery  [--product ID]       # republish HA discovery
price-tracker show       [--product ID]       # latest snapshot as JSON
price-tracker web        [--product ID] [--port N]   # status page only
price-tracker fetch      URL [--country DE] [--playwright]   # debug a page
```

Environment: `MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASSWORD MQTT_TLS DISCOVERY_PREFIX TOPIC_PREFIX
DB_PATH PRODUCTS_DIR LOG_LEVEL PER_HOST_DELAY REQUEST_TIMEOUT USER_AGENT WEB_PORT`.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

GitHub Actions: `tests.yml` on every push; `validate-sources.yml` fetches every source from GitHub's
network (daily + manual) and commits `reports/<product>.md`; `build-image.yml` pushes
`ghcr.io/neocleous/price-tracker:latest`.

## Status

See `reports/epriam-2026.md` for the latest source validation.
