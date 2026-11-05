"""Landed-cost model: sticker price in seller currency → total cost in the
reporting currency, including shipping, local VAT refund, import VAT, duty
and carrier clearance fees.

Two figures are produced for every observation:

* ``landed``            — best realistic case: local VAT deducted only when the
                          source is marked ``vat_refund: yes``.
* ``landed_no_refund``  — conservative: local VAT never deducted.

Ranking uses ``landed``; ``unknown`` refund status therefore counts as "no".
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ImportRules, Source


@dataclass
class Landed:
    landed: float
    landed_no_refund: float
    shipping: float  # in reporting currency
    import_tax: float  # in reporting currency (on the ranked case)
    refund_applied: bool
    route_mode: str
    fx_rate: float

    def as_dict(self) -> dict:
        return {
            "landed": round(self.landed, 2),
            "landed_no_refund": round(self.landed_no_refund, 2),
            "shipping": round(self.shipping, 2),
            "import_tax": round(self.import_tax, 2),
            "refund_applied": self.refund_applied,
            "route": self.route_mode,
            "fx_rate": round(self.fx_rate, 5),
        }


def compute_landed(sticker: float, currency: str, source: Source, rules: ImportRules, fx_rate: float) -> Landed:
    """``fx_rate`` = reporting-currency units per 1 unit of ``currency``."""
    route = rules.route_for(source.country, source.route)
    shipping_local = source.shipping if source.shipping is not None else route.default_shipping

    if route.mode in ("domestic", "ddp"):
        # price already carries the buyer's VAT (domestic) or VAT + clearance (ddp)
        total = (sticker + shipping_local) * fx_rate
        return Landed(total, total, shipping_local * fx_rate, 0.0, False, route.mode, fx_rate)

    def _case(refund: bool) -> tuple[float, float]:
        net = sticker / (1 + route.local_vat) if refund and route.local_vat else sticker
        goods_local = net + shipping_local
        goods = goods_local * fx_rate
        duty = goods * rules.duty
        import_vat = (goods + duty) * rules.import_vat
        fee = route.clearance_fee if route.mode == "ship" else 0.0
        return goods + duty + import_vat + fee, duty + import_vat + fee

    refund_ok = source.vat_refund == "yes" and route.refund_possible
    best, best_tax = _case(refund_ok)
    worst, _ = _case(False)
    return Landed(best, worst, shipping_local * fx_rate, best_tax, refund_ok, route.mode, fx_rate)
