"""Home Assistant MQTT discovery publisher.

One HA *device* per product. Entities (object_id → meaning):

  <pid>_total_lowest            cheapest complete set, landed, reporting currency
  <pid>_total_lowest_home       same, home-country sellers only
  <pid>_alltime_low_total       lowest complete-set price ever observed
  <pid>_<cid>_lowest            per component: cheapest landed (attrs: seller, url, sticker…)
  <pid>_<cid>_lowest_home       per component: cheapest home-country seller
  <pid>_<cid>_<sid>             per source: landed price (attrs: sticker, currency, stock…)
  <pid>_<cid>_<sid>_in_stock    per source: binary stock flag
  <pid>_last_run                timestamp of the last completed run
  <pid>_sources_ok              number of sources parsed successfully (attrs: failures)
  <pid>_problem                 binary problem flag (run failed / everything failed)
  <pid>_fx_<base>               FX rate used (reporting currency per 1 base)
  <pid>_alerts                  MQTT *event* entity: new_low | price_drop | back_in_stock | tracker_error

State is published retained so HA restarts do not blank the dashboard; events
are not retained.
"""

from __future__ import annotations

import json
import logging
import re
import time

import paho.mqtt.client as mqtt

from . import __version__
from .compute import Snapshot
from .config import Product, Settings
from .store import Store

log = logging.getLogger(__name__)

EVENT_TYPES = ["new_low", "price_drop", "back_in_stock", "tracker_error"]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")


class Publisher:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self._client: mqtt.Client | None = None

    # ------------------------------------------------------------- connection
    def connect(self):
        if self._client is not None:
            return
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"price-tracker-{int(time.time())}", protocol=mqtt.MQTTv311)
        if self.s.mqtt_user:
            c.username_pw_set(self.s.mqtt_user, self.s.mqtt_password or "")
        if self.s.mqtt_tls:
            c.tls_set()
        c.will_set(f"{self.s.topic_prefix}/bridge/availability", "offline", retain=True)
        c.reconnect_delay_set(2, 60)
        result: dict = {}

        def _on_connect(client, userdata, flags, reason_code, properties=None):
            result["rc"] = reason_code

        c.on_connect = _on_connect
        try:
            c.connect(self.s.mqtt_host, self.s.mqtt_port, keepalive=60)
        except OSError as e:
            raise ConnectionError(f"MQTT {self.s.mqtt_host}:{self.s.mqtt_port} unreachable: {e}") from e
        c.loop_start()
        deadline = time.time() + 15
        while not c.is_connected() and "rc" not in result and time.time() < deadline:
            time.sleep(0.1)
        if not c.is_connected():
            c.loop_stop()
            rc = result.get("rc")
            if rc is not None:
                # broker answered the CONNECT with a refusal (e.g. "Not authorized", "Bad user name or password")
                raise ConnectionError(
                    f"MQTT broker {self.s.mqtt_host}:{self.s.mqtt_port} refused the connection: {rc} "
                    f"(user={self.s.mqtt_user!r}) — check the Mosquitto Logins list and .env"
                )
            raise ConnectionError(f"MQTT connect to {self.s.mqtt_host}:{self.s.mqtt_port} timed out (no CONNACK in 15 s)")
        self._client = c
        self._pub(f"{self.s.topic_prefix}/bridge/availability", "online", retain=True)
        log.info("MQTT connected to %s:%s", self.s.mqtt_host, self.s.mqtt_port)

    def close(self):
        if self._client is not None:
            self._pub(f"{self.s.topic_prefix}/bridge/availability", "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    def _pub(self, topic: str, payload, retain: bool = False):
        assert self._client is not None
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, ensure_ascii=False, default=str)
        info = self._client.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=10)

    # ------------------------------------------------------------- topics
    def _t(self, pid: str, *parts: str) -> str:
        return "/".join([self.s.topic_prefix, pid, *parts])

    def _disc(self, component: str, object_id: str) -> str:
        return f"{self.s.discovery_prefix}/{component}/{object_id}/config"

    # ------------------------------------------------------------- discovery
    def publish_discovery(self, product: Product):
        self.connect()
        pid = _slug(product.id)
        cur = product.imports.reporting_currency
        avail = [
            {"topic": f"{self.s.topic_prefix}/bridge/availability"},
        ]
        device = {
            "identifiers": [f"price_tracker_{pid}"],
            "name": product.name,
            "manufacturer": "ha-price-tracker",
            "model": "Price tracker",
            "sw_version": __version__,
            "configuration_url": "https://github.com/neocleous/price-tracker",
        }
        summary = self._t(pid, "summary")
        expire = int(product.alerts.stale_after_hours * 3600)
        configs: dict[tuple[str, str], dict] = {}

        def add(component: str, suffix: str, name: str, **cfg):
            oid = f"{pid}_{suffix}"
            base = {
                "name": name,
                "unique_id": f"price_tracker_{oid}",
                "object_id": oid,
                "device": device,
                "availability": avail,
                "has_entity_name": True,
                "origin": {"name": "ha-price-tracker", "sw_version": __version__, "support_url": device["configuration_url"]},
            }
            base.update(cfg)
            configs[(component, oid)] = base

        money = {"unit_of_measurement": cur, "device_class": "monetary", "icon": "mdi:tag-outline", "suggested_display_precision": 0}

        add("sensor", "total_lowest", "Lowest complete set", state_topic=summary,
            value_template="{{ value_json.total.value if value_json.total.value is not none else 'None' }}",
            json_attributes_topic=summary, json_attributes_template="{{ value_json.total | tojson }}",
            expire_after=expire, **money)
        add("sensor", "total_lowest_home", f"Lowest complete set ({product.imports.home_country})", state_topic=summary,
            value_template="{{ value_json.total.home.value if value_json.total.home.value is not none else 'None' }}",
            json_attributes_topic=summary, json_attributes_template="{{ value_json.total.home | tojson }}",
            expire_after=expire, **money)
        add("sensor", "alltime_low_total", "All-time low complete set", state_topic=summary,
            value_template="{{ value_json.alltime.total.value if value_json.alltime.total else 'None' }}",
            json_attributes_topic=summary, json_attributes_template="{{ (value_json.alltime.total or {}) | tojson }}",
            icon="mdi:trophy-outline", **{k: v for k, v in money.items() if k != "icon"})
        add("sensor", "last_run", "Last run", state_topic=summary, value_template="{{ value_json.last_run }}",
            device_class="timestamp", icon="mdi:clock-check-outline", entity_category="diagnostic")
        add("sensor", "sources_ok", "Sources OK", state_topic=summary, value_template="{{ value_json.sources_ok }}",
            json_attributes_topic=summary, json_attributes_template="{{ value_json.health | tojson }}",
            state_class="measurement", icon="mdi:web-check", entity_category="diagnostic")
        add("binary_sensor", "problem", "Tracker problem", state_topic=summary,
            value_template="{{ 'ON' if value_json.problem else 'OFF' }}", device_class="problem",
            entity_category="diagnostic", expire_after=expire)
        for base_cur in sorted(self._currencies(product)):
            add("sensor", f"fx_{base_cur.lower()}", f"FX {base_cur}/{cur}", state_topic=summary,
                value_template=f"{{{{ value_json.fx.{base_cur} }}}}", unit_of_measurement=cur,
                suggested_display_precision=4, icon="mdi:currency-eur", entity_category="diagnostic")

        for grp, is_bundle in [(c, False) for c in product.components] + [(b, True) for b in product.bundles]:
            gid = _slug(grp.id)
            topic = self._t(pid, gid)
            label = grp.name
            add("sensor", f"{gid}_lowest", f"{label} — lowest", state_topic=topic,
                value_template="{{ value_json.lowest.landed if value_json.lowest else 'None' }}",
                json_attributes_topic=topic,
                json_attributes_template="{{ dict(value_json.lowest or {}, alltime=value_json.alltime) | tojson }}",
                expire_after=expire, **money)
            if not is_bundle:
                add("sensor", f"{gid}_lowest_home", f"{label} — lowest ({product.imports.home_country})", state_topic=topic,
                    value_template="{{ value_json.lowest_home.landed if value_json.lowest_home else 'None' }}",
                    json_attributes_topic=topic, json_attributes_template="{{ (value_json.lowest_home or {}) | tojson }}",
                    expire_after=expire, **money)
            for src in grp.sources:
                sid = _slug(src.id)
                key = f"value_json.sources['{src.id}']"
                add("sensor", f"{gid}_{sid}", f"{label} @ {src.seller}", state_topic=topic,
                    value_template=f"{{{{ {key}.landed if '{src.id}' in value_json.sources else 'None' }}}}",
                    json_attributes_topic=topic,
                    json_attributes_template=f"{{{{ (value_json.sources.get('{src.id}') or {{}}) | tojson }}}}",
                    enabled_by_default=src.enabled, **money)
                add("binary_sensor", f"{gid}_{sid}_in_stock", f"{label} @ {src.seller} in stock", state_topic=topic,
                    value_template=(
                        f"{{% set s = value_json.sources.get('{src.id}') %}}"
                        "{{ 'None' if s is none or s.in_stock is none else ('ON' if s.in_stock else 'OFF') }}"
                    ),
                    icon="mdi:package-variant-closed-check", enabled_by_default=src.enabled)

        add("event", "alerts", "Alerts", state_topic=self._t(pid, "event"), event_types=EVENT_TYPES, icon="mdi:bell-ring-outline")

        # remove entities that no longer exist in the config
        prev: list[list[str]] = self.store.get_state(f"{product.id}/mqtt/entities", [])
        current = {(c, o) for c, o in configs}
        for c, o in prev:
            if (c, o) not in current:
                self._pub(self._disc(c, o), "", retain=True)
                log.info("removed stale entity %s.%s", c, o)
        for (c, o), cfg in configs.items():
            self._pub(self._disc(c, o), cfg, retain=True)
        self.store.set_state(f"{product.id}/mqtt/entities", sorted(list(k) for k in current))
        log.info("published discovery for %d entities", len(configs))

    @staticmethod
    def _currencies(product: Product) -> set[str]:
        out: set[str] = set()
        for _gid, _b, _g, s in product.all_sources():
            c = s.currency or _default_currency(s.country)
            if c and c != product.imports.reporting_currency:
                out.add(c)
        return out

    # ------------------------------------------------------------- state
    def publish_snapshot(self, product: Product, snap: Snapshot):
        self.connect()
        pid = _slug(product.id)
        for gid, view in {**snap.components, **snap.bundles}.items():
            self._pub(self._t(pid, _slug(gid)), {**view, "alltime": snap.alltime.get(gid)}, retain=True)
        health = snap.health
        summary = {
            "total": snap.total,
            "alltime": snap.alltime,
            "last_run": snap.ts,
            "sources_ok": health.get("sources_ok"),
            "sources_total": health.get("sources_total"),
            "problem": bool(health.get("problem")),
            "health": health,
            "fx": snap.fx,
        }
        self._pub(self._t(pid, "summary"), summary, retain=True)
        for ev in snap.events:
            self._pub(self._t(pid, "event"), ev, retain=False)
            time.sleep(0.3)  # let HA register distinct events
        log.info("published snapshot: total=%s events=%d", snap.total.get("value"), len(snap.events))

    def publish_error(self, product: Product, message: str):
        self.connect()
        pid = _slug(product.id)
        self._pub(self._t(pid, "event"), {"event_type": "tracker_error", "product": product.id, "message": message[:500]})


def _default_currency(country: str) -> str | None:
    return {
        "CH": "CHF", "LI": "CHF", "GB": "GBP", "DK": "DKK", "SE": "SEK", "NO": "NOK", "PL": "PLN", "CZ": "CZK",
        "HU": "HUF", "US": "USD",
    }.get(country, "EUR")
