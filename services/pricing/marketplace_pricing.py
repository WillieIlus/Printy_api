from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from django.db import OperationalError, ProgrammingError

from pricing.models import ShopPricingSettings


DEFAULT_BROKER_MARGIN_PERCENT = Decimal("30.00")
DEFAULT_SERVICE_MARGIN_PERCENT = Decimal("30.00")
MONEY_QUANTIZER = Decimal("0.01")
PERCENT_QUANTIZER = Decimal("0.01")


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return default


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTIZER, rounding=ROUND_HALF_UP)


def quantize_percent(value: Decimal) -> Decimal:
    return value.quantize(PERCENT_QUANTIZER, rounding=ROUND_HALF_UP)


def get_marketplace_margin_settings(shop=None) -> dict[str, Any]:
    defaults = {
        "broker_margin_percent": DEFAULT_BROKER_MARGIN_PERCENT,
        "service_margin_percent": DEFAULT_SERVICE_MARGIN_PERCENT,
        "broker_margin_locked": True,
        "service_margin_locked": True,
        "is_active": True,
        "scope": "default",
    }
    if shop is None:
        return defaults

    try:
        settings = (
            ShopPricingSettings.objects.filter(shop=shop, is_active=True)
            .order_by("-updated_at", "-id")
            .first()
        )
    except (OperationalError, ProgrammingError):
        return defaults | {"scope": "shop_default", "shop_id": getattr(shop, "id", None)}
    if settings is None:
        return defaults | {"scope": "shop_default", "shop_id": shop.id}

    return {
        "broker_margin_percent": _decimal(settings.broker_margin_percent, DEFAULT_BROKER_MARGIN_PERCENT),
        "service_margin_percent": _decimal(settings.service_margin_percent, DEFAULT_SERVICE_MARGIN_PERCENT),
        "broker_margin_locked": bool(settings.broker_margin_locked),
        "service_margin_locked": bool(settings.service_margin_locked),
        "is_active": bool(settings.is_active),
        "scope": "shop",
        "shop_id": shop.id,
        "settings_id": settings.id,
    }


def calculate_client_price(
    base_price: Any,
    broker_margin_percent: Any = DEFAULT_BROKER_MARGIN_PERCENT,
    service_margin_percent: Any = DEFAULT_SERVICE_MARGIN_PERCENT,
) -> dict[str, Decimal]:
    base_amount = quantize_money(_decimal(base_price))
    broker_percent = quantize_percent(_decimal(broker_margin_percent, DEFAULT_BROKER_MARGIN_PERCENT))
    service_percent = quantize_percent(_decimal(service_margin_percent, DEFAULT_SERVICE_MARGIN_PERCENT))
    broker_amount = quantize_money(base_amount * broker_percent / Decimal("100"))
    service_amount = quantize_money(base_amount * service_percent / Decimal("100"))
    client_price = quantize_money(base_amount + broker_amount + service_amount)
    total_margin_amount = quantize_money(broker_amount + service_amount)
    total_margin_percent = quantize_percent(broker_percent + service_percent)
    multiplier = quantize_percent(Decimal("1") + (total_margin_percent / Decimal("100")))
    return {
        "base_price": base_amount,
        "broker_margin_percent": broker_percent,
        "broker_margin_amount": broker_amount,
        "service_margin_percent": service_percent,
        "service_margin_amount": service_amount,
        "total_margin_percent": total_margin_percent,
        "total_margin_amount": total_margin_amount,
        "client_price": client_price,
        "multiplier": multiplier,
    }


def serialize_marketplace_pricing(summary: dict[str, Decimal], *, currency: str = "KES") -> dict[str, Any]:
    return {
        "currency": currency,
        "base_price": str(summary["base_price"]),
        "broker_margin_percent": str(summary["broker_margin_percent"]),
        "broker_margin_amount": str(summary["broker_margin_amount"]),
        "service_margin_percent": str(summary["service_margin_percent"]),
        "service_margin_amount": str(summary["service_margin_amount"]),
        "total_margin_percent": str(summary["total_margin_percent"]),
        "total_margin_amount": str(summary["total_margin_amount"]),
        "client_price": str(summary["client_price"]),
        "multiplier": str(summary["multiplier"]),
        "lines": [
            {
                "key": "base_price",
                "label": "Your shop price",
                "amount": str(summary["base_price"]),
            },
            {
                "key": "broker_margin",
                "label": f"Broker margin ({summary['broker_margin_percent']}%)",
                "amount": str(summary["broker_margin_amount"]),
            },
            {
                "key": "service_margin",
                "label": f"Printy service ({summary['service_margin_percent']}%)",
                "amount": str(summary["service_margin_amount"]),
            },
            {
                "key": "client_price",
                "label": "Client price",
                "amount": str(summary["client_price"]),
            },
        ],
    }


def build_marketplace_pricing_summary(*, base_price: Any, shop=None, currency: str = "KES") -> dict[str, Any]:
    settings = get_marketplace_margin_settings(shop)
    summary = calculate_client_price(
        base_price,
        broker_margin_percent=settings["broker_margin_percent"],
        service_margin_percent=settings["service_margin_percent"],
    )
    return {
        **serialize_marketplace_pricing(summary, currency=currency),
        "settings": {
            "broker_margin_percent": str(settings["broker_margin_percent"]),
            "service_margin_percent": str(settings["service_margin_percent"]),
            "broker_margin_locked": settings["broker_margin_locked"],
            "service_margin_locked": settings["service_margin_locked"],
            "is_active": settings["is_active"],
            "scope": settings["scope"],
            "shop_id": settings.get("shop_id"),
            "settings_id": settings.get("settings_id"),
        },
    }


def apply_marketplace_pricing_to_preview(preview: dict[str, Any], *, shop=None) -> dict[str, Any]:
    payload = deepcopy(preview)
    totals = dict(payload.get("totals") or {})
    currency = payload.get("currency") or "KES"
    base_price = totals.get("grand_total") or totals.get("subtotal") or "0.00"
    marketplace_pricing = build_marketplace_pricing_summary(
        base_price=base_price,
        shop=shop,
        currency=currency,
    )

    totals["shop_total"] = str(quantize_money(_decimal(base_price)))
    totals["production_total"] = totals["shop_total"]
    totals["client_price"] = marketplace_pricing["client_price"]
    totals["broker_margin_amount"] = marketplace_pricing["broker_margin_amount"]
    totals["service_margin_amount"] = marketplace_pricing["service_margin_amount"]
    totals["total_marketplace_margin"] = marketplace_pricing["total_margin_amount"]
    totals["grand_total"] = marketplace_pricing["client_price"]
    payload["totals"] = totals

    breakdown = dict(payload.get("breakdown") or {})
    breakdown["marketplace_pricing"] = marketplace_pricing
    payload["breakdown"] = breakdown
    payload["marketplace_pricing"] = marketplace_pricing
    return payload
