"""Product configuration schema and loader.

A product file (``products/<id>.yaml``) fully describes what is tracked, from
where, and how prices are normalised to a landed cost in the reporting
currency. Nothing product-specific lives in code.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Parser = Literal["jsonld", "meta", "css", "regex", "playwright"]
VatRefund = Literal["yes", "no", "unknown"]


class Match(BaseModel):
    """Optional checks that the fetched page is the product we think it is.

    On listing/variant pages that expose several schema.org Products, these
    also select which Product block to read.
    """

    sku: list[str] = Field(default_factory=list)  # substring match, any of
    sku_regex: str | None = None  # full regex on the offer sku
    name_regex: str | None = None
    name_exclude_regex: str | None = None

    def name_ok(self, name: str | None) -> bool:
        if name is None:
            return not self.name_regex
        if self.name_regex and not re.search(self.name_regex, name, re.I):
            return False
        if self.name_exclude_regex and re.search(self.name_exclude_regex, name, re.I):
            return False
        return True


class Source(BaseModel):
    id: str
    seller: str
    url: str
    country: str = Field(min_length=2, max_length=2)
    currency: str | None = None  # override when the page omits it
    parser: Parser = "jsonld"
    selector: str | None = None  # css
    pattern: str | None = None  # regex, first group = price
    stock_selector: str | None = None
    stock_in_regex: str | None = None
    vat_refund: VatRefund = "unknown"
    shipping: float | None = None  # in the source currency, to the route's delivery point
    interval_hours: float = 1.0
    enabled: bool = True
    notes: str | None = None
    match: Match = Field(default_factory=Match)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("country")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _parser_args(self) -> "Source":
        if self.parser == "css" and not self.selector:
            raise ValueError(f"source {self.id}: css parser needs 'selector'")
        if self.parser == "regex" and not self.pattern:
            raise ValueError(f"source {self.id}: regex parser needs 'pattern'")
        return self


class Bounds(BaseModel):
    """Sanity bounds in the reporting currency; observations outside are rejected."""

    min: float
    max: float


class Component(BaseModel):
    id: str
    name: str
    required: bool = True
    bounds: Bounds
    sources: list[Source] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]+", v):
            raise ValueError("component id must be [a-z0-9_]+")
        return v


class Bundle(BaseModel):
    """A seller SKU that covers several components at one price."""

    id: str
    name: str
    covers: list[str]
    bounds: Bounds
    sources: list[Source] = Field(default_factory=list)


class Route(BaseModel):
    """How goods from a given seller country reach the buyer.

    ``collect``: picked up in that country (e.g. a parcel box across the
    border) and self-imported — local VAT may be refunded by the seller,
    import VAT is due on the net value, no carrier clearance fee.

    ``ship``: shipped to the home country — seller normally exports net of
    local VAT, carrier charges import VAT plus a clearance fee.

    ``domestic``: home-country seller, sticker is landed.
    """

    mode: Literal["domestic", "collect", "ship"]
    local_vat: float = 0.0
    default_shipping: float = 0.0  # in the seller's currency
    clearance_fee: float = 0.0  # in the reporting currency
    refund_possible: bool = True
    delivery_note: str | None = None  # shown in alerts, e.g. "Deliver to the German parcel box"


class ImportRules(BaseModel):
    home_country: str = "CH"
    reporting_currency: str = "CHF"
    import_vat: float = 0.081
    duty: float = 0.0
    routes: dict[str, Route]
    default_route: Route

    home_delivery_note: str | None = None  # shown in alerts for home-country sellers

    def route_for(self, country: str) -> Route:
        if country == self.home_country:
            return Route(mode="domestic", delivery_note=self.home_delivery_note)
        return self.routes.get(country, self.default_route)

    def delivery_note(self, country: str) -> str:
        r = self.route_for(country)
        if r.delivery_note:
            return r.delivery_note
        return {"domestic": f"Delivery within {self.home_country}", "collect": f"Collect in {country}",
                "ship": f"Ships from {country} to {self.home_country}"}[r.mode]


class Alerts(BaseModel):
    price_drop_pct: float = 5.0
    require_in_stock: bool = True
    stale_after_hours: float = 36.0


class Schedule(BaseModel):
    timezone: str = "Europe/Zurich"
    hours: list[int] = Field(default_factory=lambda: [0, *range(7, 24)])
    minute: int = 0

    @field_validator("hours", mode="before")
    @classmethod
    def _parse_hours(cls, v):
        if isinstance(v, str):
            out: set[int] = set()
            for part in v.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-")
                    out.update(range(int(a), int(b) + 1))
                elif part:
                    out.add(int(part))
            return sorted(out)
        return v


class Product(BaseModel):
    id: str
    name: str
    description: str | None = None
    imports: ImportRules
    alerts: Alerts = Field(default_factory=Alerts)
    schedule: Schedule = Field(default_factory=Schedule)
    components: list[Component]
    bundles: list[Bundle] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)  # shown on the status page

    @model_validator(mode="after")
    def _check(self) -> "Product":
        ids = [c.id for c in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate component ids")
        for b in self.bundles:
            unknown = set(b.covers) - set(ids)
            if unknown:
                raise ValueError(f"bundle {b.id} covers unknown components {unknown}")
        seen: set[str] = set()
        for grp in (*self.components, *self.bundles):
            for s in grp.sources:
                key = f"{grp.id}/{s.id}"
                if key in seen:
                    raise ValueError(f"duplicate source id {key}")
                seen.add(key)
        return self

    def component(self, cid: str) -> Component:
        return next(c for c in self.components if c.id == cid)

    def all_sources(self):
        """Yield (group_id, is_bundle, group, source) for every enabled source."""
        for c in self.components:
            for s in c.sources:
                if s.enabled:
                    yield c.id, False, c, s
        for b in self.bundles:
            for s in b.sources:
                if s.enabled:
                    yield b.id, True, b, s


class Settings(BaseModel):
    """Runtime settings from environment variables (never from the product file)."""

    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str | None = None
    mqtt_password: str | None = None
    mqtt_tls: bool = False
    discovery_prefix: str = "homeassistant"
    topic_prefix: str = "price_tracker"
    db_path: Path = Path("/data/tracker.sqlite")
    products_dir: Path = Path("/config/products")
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    request_timeout: float = 30.0
    per_host_delay: float = 2.0
    log_level: str = "INFO"
    web_port: int = 8099  # 0 disables the status page

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        kw: dict = {}
        mapping = {
            "MQTT_HOST": "mqtt_host",
            "MQTT_PORT": "mqtt_port",
            "MQTT_USER": "mqtt_user",
            "MQTT_PASSWORD": "mqtt_password",
            "MQTT_TLS": "mqtt_tls",
            "DISCOVERY_PREFIX": "discovery_prefix",
            "TOPIC_PREFIX": "topic_prefix",
            "DB_PATH": "db_path",
            "PRODUCTS_DIR": "products_dir",
            "USER_AGENT": "user_agent",
            "REQUEST_TIMEOUT": "request_timeout",
            "PER_HOST_DELAY": "per_host_delay",
            "LOG_LEVEL": "log_level",
            "WEB_PORT": "web_port",
        }
        for k, attr in mapping.items():
            if k in env and env[k] != "":
                kw[attr] = env[k]
        if "mqtt_tls" in kw:
            kw["mqtt_tls"] = str(kw["mqtt_tls"]).lower() in ("1", "true", "yes")
        return cls(**kw)


def load_product(path: Path | str) -> Product:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Product.model_validate(data)


def load_products(directory: Path | str) -> list[Product]:
    directory = Path(directory)
    products = []
    for p in sorted(directory.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        products.append(load_product(p))
    return products
