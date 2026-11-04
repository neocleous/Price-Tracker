"""Extract price / currency / availability from a product page.

Strategy order (``parser: jsonld`` tries 1→2→3 automatically):
1. schema.org JSON-LD ``Product``/``Offer`` blocks — shop-agnostic, stable.
2. ``<meta>`` price tags (OpenGraph ``product:price:amount``, ``itemprop=price``).
3. Explicit CSS selector or regex supplied in the source config.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .config import Match

_IN_STOCK = {
    "instock",
    "instoreonly",
    "onlineonly",
    "limitedavailability",
    "presale",
    "preorder",
}
_OUT_OF_STOCK = {"outofstock", "soldout", "discontinued", "backorder"}


@dataclass
class Offer:
    price: float
    currency: str | None
    in_stock: bool | None = None
    name: str | None = None
    sku: str | None = None
    method: str = ""
    raw: dict = field(default_factory=dict)


class ParseError(Exception):
    pass


# --------------------------------------------------------------------------- numbers


_NUM_CLEAN = re.compile(r"[^\d.,'’\s\-–]")


def parse_price(text: str) -> float:
    """Parse localised price strings.

    Handles: ``1'299.00``, ``1’299.–``, ``1.299,00 €``, ``€1,299.00``,
    ``CHF 1'997.00``, ``1299``, ``2 299,00``, ``1299.-``.
    """
    if text is None:
        raise ParseError("empty price")
    s = str(text).strip()
    s = re.sub(r"[.,]\s*[-–]+\s*$", ".00", s)  # 1299.– / 1299,- -> 1299.00
    s = _NUM_CLEAN.sub("", s).replace("’", "'")
    s = re.sub(r"[-–]", "", s)
    s = re.sub(r"(?<=\d)['\s](?=\d{3}\b)", "", s)  # thousands: 1'299 / 2 299
    s = s.strip()
    if not s:
        raise ParseError(f"no digits in {text!r}")
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")  # 1.299,00
        else:
            s = s.replace(",", "")  # 1,299.00
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head}.{tail}" if len(tail) in (1, 2) else head + tail
    elif "." in s:
        head, _, tail = s.rpartition(".")
        s = f"{head}.{tail}" if len(tail) in (1, 2) else head + tail
    try:
        value = float(s)
    except ValueError as e:  # pragma: no cover
        raise ParseError(f"cannot parse price {text!r}") from e
    if value <= 0:
        raise ParseError(f"non-positive price {text!r}")
    return value


def availability_flag(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name") or ""
    v = str(value).lower()
    v = v.rsplit("/", 1)[-1].replace("_", "")
    if v in _IN_STOCK:
        return True
    if v in _OUT_OF_STOCK:
        return False
    if "instock" in v or "available" in v or "lieferbar" in v or "auf lager" in v:
        return True
    if "outofstock" in v or "nicht" in v or "ausverkauft" in v or "soldout" in v:
        return False
    return None


# --------------------------------------------------------------------------- json-ld


def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        txt = tag.string or tag.get_text() or ""
        txt = txt.strip()
        if not txt:
            continue
        try:
            data = json.loads(txt, strict=False)
        except json.JSONDecodeError:
            # tolerate trailing commas / HTML comments
            txt2 = re.sub(r"<!--.*?-->", "", txt, flags=re.S)
            txt2 = re.sub(r",\s*([}\]])", r"\1", txt2)
            try:
                data = json.loads(txt2, strict=False)
            except json.JSONDecodeError:
                continue
        yield data


def _walk(node, depth=0):
    """Yield every dict in a JSON-LD structure (handles @graph, lists, nesting)."""
    if depth > 8:
        return
    if isinstance(node, list):
        for n in node:
            yield from _walk(n, depth + 1)
    elif isinstance(node, dict):
        yield node
        for k, v in node.items():
            if k in ("offers", "@graph", "itemListElement", "hasVariant", "isVariantOf", "mainEntity", "item"):
                yield from _walk(v, depth + 1)


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    if isinstance(t, str):
        t = [t]
    return {str(x).rsplit("/", 1)[-1].lower() for x in t}


def _offers_of(product: dict) -> list[dict]:
    offers = product.get("offers")
    if offers is None:
        return []
    if isinstance(offers, dict):
        if "aggregateoffer" in _types(offers) and isinstance(offers.get("offers"), list):
            return [o for o in offers["offers"] if isinstance(o, dict)] or [offers]
        return [offers]
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def _offer_price(o: dict) -> tuple[float | None, str | None]:
    for key in ("price", "lowPrice", "highPrice"):
        if key in o and o[key] not in (None, ""):
            try:
                return parse_price(str(o[key])), o.get("priceCurrency")
            except ParseError:
                pass
    spec = o.get("priceSpecification")
    specs = spec if isinstance(spec, list) else [spec] if spec else []
    for sp in specs:
        if isinstance(sp, dict) and sp.get("price") not in (None, ""):
            try:
                return parse_price(str(sp["price"])), sp.get("priceCurrency") or o.get("priceCurrency")
            except ParseError:
                pass
    return None, None


def offers_from_jsonld(html: str | BeautifulSoup) -> list[Offer]:
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")
    found: list[Offer] = []
    for data in _iter_jsonld(soup):
        for node in _walk(data):
            types = _types(node)
            if not types & {"product", "productmodel", "individualproduct", "productgroup", "vehicle"}:
                continue
            name = node.get("name")
            sku = node.get("sku") or node.get("mpn") or node.get("gtin13") or node.get("productID")
            for o in _offers_of(node):
                price, cur = _offer_price(o)
                if price is None:
                    continue
                found.append(
                    Offer(
                        price=price,
                        currency=(cur or "").upper() or None,
                        in_stock=availability_flag(o.get("availability")),
                        name=o.get("name") or name,
                        sku=str(o.get("sku") or sku or "") or None,
                        method="jsonld",
                        raw={k: o.get(k) for k in ("price", "priceCurrency", "availability", "sku", "url")},
                    )
                )
    return found


# --------------------------------------------------------------------------- meta tags


def offers_from_meta(soup: BeautifulSoup, default_currency: str | None) -> list[Offer]:
    out: list[Offer] = []
    pairs = [
        ("product:price:amount", "product:price:currency"),
        ("og:price:amount", "og:price:currency"),
        ("product:sale_price:amount", "product:sale_price:currency"),
    ]
    for amt, cur in pairs:
        tag = soup.find("meta", attrs={"property": amt}) or soup.find("meta", attrs={"name": amt})
        if tag and tag.get("content"):
            ctag = soup.find("meta", attrs={"property": cur}) or soup.find("meta", attrs={"name": cur})
            try:
                out.append(
                    Offer(
                        price=parse_price(tag["content"]),
                        currency=(ctag["content"].upper() if ctag and ctag.get("content") else default_currency),
                        method="meta",
                    )
                )
            except ParseError:
                pass
    for tag in soup.select('[itemprop="price"]'):
        val = tag.get("content") or tag.get_text(" ", strip=True)
        try:
            ctag = soup.select_one('[itemprop="priceCurrency"]')
            out.append(
                Offer(
                    price=parse_price(val),
                    currency=((ctag.get("content") or ctag.get_text(strip=True)).upper() if ctag else default_currency),
                    in_stock=availability_flag(
                        (soup.select_one('[itemprop="availability"]') or {}).get("href")
                        or (soup.select_one('[itemprop="availability"]') or {}).get("content")
                    ),
                    method="microdata",
                )
            )
        except ParseError:
            pass
    return out


# --------------------------------------------------------------------------- css / regex


def offer_from_css(soup: BeautifulSoup, selector: str, default_currency: str | None,
                   stock_selector: str | None = None, stock_in_regex: str | None = None) -> Offer:
    el = soup.select_one(selector)
    if el is None:
        raise ParseError(f"selector {selector!r} matched nothing")
    text = el.get("content") or el.get_text(" ", strip=True)
    in_stock = None
    if stock_selector:
        st = soup.select_one(stock_selector)
        if st is not None:
            stext = st.get_text(" ", strip=True)
            in_stock = bool(re.search(stock_in_regex, stext, re.I)) if stock_in_regex else availability_flag(stext)
    return Offer(price=parse_price(text), currency=default_currency, in_stock=in_stock, method="css", raw={"text": text})


def offer_from_regex(html: str, pattern: str, default_currency: str | None) -> Offer:
    m = re.search(pattern, html, re.S | re.I)
    if not m:
        raise ParseError(f"pattern {pattern!r} matched nothing")
    text = m.group(1) if m.groups() else m.group(0)
    return Offer(price=parse_price(text), currency=default_currency, method="regex", raw={"text": text})


# --------------------------------------------------------------------------- selection


def select_offer(offers: list[Offer], match: Match, currency_hint: str | None) -> Offer:
    """Pick the offer that matches the configured product; lowest price among ties."""
    if not offers:
        raise ParseError("no offers found")
    candidates = offers
    if match.sku:
        skus = {s.lower() for s in match.sku}
        by_sku = [o for o in candidates if o.sku and any(s in o.sku.lower() for s in skus)]
        if by_sku:
            candidates = by_sku
    if match.sku_regex:
        by_rx = [o for o in candidates if o.sku and re.search(match.sku_regex, o.sku, re.I)]
        if by_rx:
            candidates = by_rx
        else:
            raise ParseError(
                f"no offer sku matched /{match.sku_regex}/; skus seen: "
                + ", ".join(sorted({str(o.sku) for o in offers}))[:300]
            )
    if match.name_regex or match.name_exclude_regex:
        by_name = [o for o in candidates if match.name_ok(o.name)]
        if by_name:
            candidates = by_name
        elif match.name_regex:
            raise ParseError(
                f"no offer name matched /{match.name_regex}/; names seen: "
                + ", ".join(sorted({str(o.name) for o in offers}))[:300]
            )
    if currency_hint:
        same = [o for o in candidates if (o.currency or currency_hint) == currency_hint]
        if same:
            candidates = same
    # prefer offers that are in stock, then lowest price
    candidates = sorted(candidates, key=lambda o: (o.in_stock is False, o.price))
    return candidates[0]


def extract(html: str, parser: str, *, default_currency: str | None, match: Match,
            selector: str | None = None, pattern: str | None = None,
            stock_selector: str | None = None, stock_in_regex: str | None = None) -> Offer:
    """Entry point used by the fetcher. ``parser`` is the configured strategy."""
    soup = BeautifulSoup(html, "lxml")
    if parser in ("jsonld", "playwright"):
        offers = offers_from_jsonld(soup)
        if offers:
            return select_offer(offers, match, default_currency)
        offers = offers_from_meta(soup, default_currency)
        if offers:
            return select_offer(offers, match, default_currency)
        if selector:
            return offer_from_css(soup, selector, default_currency, stock_selector, stock_in_regex)
        if pattern:
            return offer_from_regex(html, pattern, default_currency)
        raise ParseError("no JSON-LD/meta price found and no selector/pattern configured")
    if parser == "meta":
        offers = offers_from_meta(soup, default_currency)
        return select_offer(offers, match, default_currency)
    if parser == "css":
        return offer_from_css(soup, selector, default_currency, stock_selector, stock_in_regex)
    if parser == "regex":
        return offer_from_regex(html, pattern, default_currency)
    raise ParseError(f"unknown parser {parser}")


def page_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None
