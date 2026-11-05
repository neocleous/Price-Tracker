import pytest

from tracker.config import ImportRules, Route, Source
from tracker.landed import compute_landed

RULES = ImportRules(
    home_country="CH",
    reporting_currency="CHF",
    import_vat=0.081,
    duty=0.0,
    routes={
        "DE": Route(mode="collect", local_vat=0.19, default_shipping=0.0),
        "AT": Route(mode="ship", local_vat=0.20, default_shipping=29.0, clearance_fee=25.0),
    },
    default_route=Route(mode="ship", local_vat=0.20, default_shipping=40.0, clearance_fee=25.0, refund_possible=False),
)


def src(country, vat_refund="unknown", shipping=None):
    return Source(id="s", seller="s", url="https://x", country=country, vat_refund=vat_refund, shipping=shipping)


def test_domestic_is_sticker_plus_shipping():
    l = compute_landed(1000.0, "CHF", src("CH", shipping=9.9), RULES, 1.0)
    assert l.landed == pytest.approx(1009.9)
    assert l.landed_no_refund == pytest.approx(1009.9)
    assert l.import_tax == 0


def test_collect_de_with_refund():
    # 1190 EUR incl. 19% → 1000 net → +8.1% CH VAT = 1081 EUR → ×0.94 = 1016.14 CHF
    l = compute_landed(1190.0, "EUR", src("DE", "yes"), RULES, 0.94)
    assert l.landed == pytest.approx(1016.14, abs=0.01)
    assert l.refund_applied is True
    # conservative column never deducts: 1190 × 1.081 × 0.94
    assert l.landed_no_refund == pytest.approx(1190 * 1.081 * 0.94, abs=0.01)


def test_collect_de_unknown_refund_is_conservative():
    l = compute_landed(1190.0, "EUR", src("DE", "unknown"), RULES, 0.94)
    assert l.landed == pytest.approx(l.landed_no_refund)
    assert l.refund_applied is False


def test_ship_route_adds_clearance_fee_and_shipping():
    l = compute_landed(1200.0, "EUR", src("AT", "yes"), RULES, 0.94)
    net = 1200 / 1.20 + 29.0
    expected = net * 0.94 * 1.081 + 25.0
    assert l.landed == pytest.approx(expected, abs=0.01)


def test_default_route_refund_not_possible():
    l = compute_landed(1200.0, "EUR", src("FR", "yes"), RULES, 0.94)
    assert l.refund_applied is False
    assert l.landed == pytest.approx((1200 + 40) * 0.94 * 1.081 + 25.0, abs=0.01)


def test_ddp_route_is_sticker_plus_shipping():
    rules = ImportRules(
        home_country="CH", reporting_currency="CHF", import_vat=0.081,
        routes={"shop_ddp": Route(mode="ddp", default_shipping=12.95)},
        default_route=Route(mode="collect", local_vat=0.2, refund_possible=False),
    )
    s = Source(id="s", seller="s", url="https://x", country="DE", route="shop_ddp", currency="CHF")
    l = compute_landed(1749.95, "CHF", s, rules, 1.0)
    assert l.landed == pytest.approx(1762.90) and l.import_tax == 0 and l.route_mode == "ddp"
    assert l.shipping == pytest.approx(12.95)
