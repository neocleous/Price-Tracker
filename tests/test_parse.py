import pytest

from tracker.config import Match
from tracker.parse import ParseError, extract, offers_from_jsonld, parse_price, select_offer


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1'299.00", 1299.0),
        ("CHF 1’997.00", 1997.0),
        ("1’299.–", 1299.0),
        ("1299.-", 1299.0),
        ("1.299,00 €", 1299.0),
        ("€1,299.00", 1299.0),
        ("1299", 1299.0),
        ("2 299,00", 2299.0),
        ("1.299", 1299.0),
        ("1,299", 1299.0),
        ("899,9", 899.9),
        ("  CHF 1'448.00 ", 1448.0),
        ("1447.80", 1447.8),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == pytest.approx(expected)


def test_parse_price_rejects_garbage():
    with pytest.raises(ParseError):
        parse_price("ab")
    with pytest.raises(ParseError):
        parse_price("0")


JSONLD_SINGLE = """
<html><head><title>X</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Cybex e-Priam Frame","sku":"526000243",
 "offers":{"@type":"Offer","price":"1299.00","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}
</script></head><body></body></html>
"""

JSONLD_VARIANTS = """
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"ProductGroup","name":"e-Priam Style","hasVariant":[
 {"@type":"Product","name":"e-Priam Style Frame+Seat","sku":"526000243_526000297",
  "offers":{"@type":"Offer","price":1488.0,"priceCurrency":"CHF","availability":"http://schema.org/InStock"}},
 {"@type":"Product","name":"e-Priam Style Frame+Seat+Cot","sku":"526000243_526000297_526000353",
  "offers":{"@type":"Offer","price":1947.0,"priceCurrency":"CHF","availability":"http://schema.org/OutOfStock"}}
]}
</script>
"""

JSONLD_AGGREGATE = """
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"WebPage"},{"@type":"Product","name":"Cloud T i-Size",
 "offers":{"@type":"AggregateOffer","lowPrice":"259.99","highPrice":"299.99","priceCurrency":"EUR","offerCount":3}}]}
</script>
"""

META_ONLY = """
<html><head>
<meta property="og:title" content="Base T">
<meta property="product:price:amount" content="199.95">
<meta property="product:price:currency" content="CHF">
</head><body><span class="price">CHF 199.95</span></body></html>
"""

CSS_ONLY = """
<html><body><div class="pdp"><span data-test="price">1’299.–</span>
<div class="stock">Sofort lieferbar</div></div></body></html>
"""


def test_jsonld_single():
    o = extract(JSONLD_SINGLE, "jsonld", default_currency="EUR", match=Match())
    assert o.price == 1299.0 and o.currency == "EUR" and o.in_stock is True and o.method == "jsonld"
    assert o.sku == "526000243"


def test_jsonld_variants_select_by_sku():
    offers = offers_from_jsonld(JSONLD_VARIANTS)
    assert len(offers) == 2
    o = select_offer(offers, Match(sku=["526000353"]), "CHF")
    assert o.price == 1947.0 and o.in_stock is False
    # without a sku match the in-stock, cheapest one wins
    o2 = select_offer(offers, Match(), "CHF")
    assert o2.price == 1488.0


def test_jsonld_variants_select_by_name():
    offers = offers_from_jsonld(JSONLD_VARIANTS)
    o = select_offer(offers, Match(name_regex=r"\+Cot"), "CHF")
    assert o.price == 1947.0
    with pytest.raises(ParseError):
        select_offer(offers, Match(name_regex=r"Lite Cot"), "CHF")


def test_jsonld_aggregate_offer_uses_low_price():
    o = extract(JSONLD_AGGREGATE, "jsonld", default_currency="EUR", match=Match())
    assert o.price == pytest.approx(259.99)


def test_meta_fallback():
    o = extract(META_ONLY, "jsonld", default_currency=None, match=Match())
    assert o.price == pytest.approx(199.95) and o.currency == "CHF" and o.method == "meta"


def test_css_with_stock():
    o = extract(CSS_ONLY, "css", default_currency="CHF", match=Match(), selector='[data-test="price"]',
                stock_selector=".stock", stock_in_regex=r"lieferbar")
    assert o.price == 1299.0 and o.in_stock is True


def test_regex():
    o = extract('<script>var p = {"price": "1.447,80"}</script>', "regex", default_currency="EUR", match=Match(),
                pattern=r'"price":\s*"([\d.,]+)"')
    assert o.price == pytest.approx(1447.80)


def test_no_price_raises():
    with pytest.raises(ParseError):
        extract("<html><body>nothing</body></html>", "jsonld", default_currency="EUR", match=Match())
