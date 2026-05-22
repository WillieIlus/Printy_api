"""Partner quote builder helpers on top of the existing quote workflow."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction

from api.visibility import TOPOLOGY_MANAGED
from jobs.payment_services import calculate_partner_job_split, get_default_platform_service_percent
from production.models import Customer
from quotes.services_workflow import create_quote_response, save_quote_draft, send_quote_draft_to_shops
from services.pricing.projections import project_broker_projection
from shops.models import Shop


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _money(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _extract_shop_preview(pricing_snapshot: dict[str, Any], shop: Shop) -> dict[str, Any]:
    for entry in pricing_snapshot.get("selected_shops") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == shop.id or entry.get("slug") == shop.slug:
            return entry
    return pricing_snapshot


def build_partner_quote_preview(*, pricing_snapshot: dict[str, Any], shop: Shop, partner_markup: Decimal) -> dict[str, Any]:
    shop_preview = _extract_shop_preview(pricing_snapshot, shop)
    raw_payload = _as_dict(shop_preview.get("preview")) or shop_preview
    broker_projection = project_broker_projection(raw_payload)
    production_estimate = _money(broker_projection.get("production_estimate"))
    minimum_price = production_estimate
    suggested_max = production_estimate + max(partner_markup, production_estimate * Decimal("0.35"))
    platform_service_percent = get_default_platform_service_percent()
    platform_service_amount = (production_estimate * platform_service_percent / Decimal("100")).quantize(Decimal("0.01"))
    final_client_price = production_estimate + partner_markup + platform_service_amount
    broker_projection.update(
        {
            "production_estimate": str(production_estimate.quantize(Decimal("0.01"))),
            "suggested_selling_range": {
                "min": str(minimum_price.quantize(Decimal("0.01"))),
                "max": str(suggested_max.quantize(Decimal("0.01"))),
            },
            "broker_markup": str(partner_markup.quantize(Decimal("0.01"))),
            "platform_service_amount": str(platform_service_amount),
            "platform_service_percent": str(platform_service_percent),
            "client_price": str(final_client_price.quantize(Decimal("0.01"))),
            "margin": str(partner_markup.quantize(Decimal("0.01"))),
        }
    )
    return broker_projection


def validate_partner_markup(*, pricing_snapshot: dict[str, Any], shop: Shop, partner_markup: Decimal) -> None:
    preview = build_partner_quote_preview(pricing_snapshot=pricing_snapshot, shop=shop, partner_markup=partner_markup)
    production_estimate = _money(preview.get("production_estimate"))
    if partner_markup < 0:
        raise ValueError("Partner markup cannot be negative.")
    if production_estimate <= 0:
        raise ValueError("Production price is not available yet for the selected shop.")
    if production_estimate > 0 and partner_markup > (production_estimate * Decimal("1.50")):
        raise ValueError("Partner markup exceeds the current guardrail.")


def get_or_create_partner_customer(
    *,
    shop: Shop,
    partner_user,
    client_name: str,
    client_email: str = "",
    client_phone: str = "",
) -> Customer:
    customer, created = Customer.objects.get_or_create(
        shop=shop,
        name=client_name or client_email or client_phone or f"Partner client {partner_user.id}",
        defaults={
            "email": client_email,
            "phone": client_phone,
            "relationship_owner_type": Customer.RelationshipOwnerType.USER,
            "relationship_owner_user": partner_user,
            "acquisition_source": Customer.AcquisitionSource.PARTNER,
        },
    )
    changed = False
    if not created:
        if client_email and customer.email != client_email:
            customer.email = client_email
            changed = True
        if client_phone and customer.phone != client_phone:
            customer.phone = client_phone
            changed = True
        if customer.relationship_owner_type != Customer.RelationshipOwnerType.USER:
            customer.relationship_owner_type = Customer.RelationshipOwnerType.USER
            changed = True
        if customer.relationship_owner_user_id != partner_user.id:
            customer.relationship_owner_user = partner_user
            changed = True
        if customer.acquisition_source != Customer.AcquisitionSource.PARTNER:
            customer.acquisition_source = Customer.AcquisitionSource.PARTNER
            changed = True
        if changed:
            customer.save(update_fields=["email", "phone", "relationship_owner_type", "relationship_owner_user", "acquisition_source", "updated_at"])
    return customer


@transaction.atomic
def create_partner_quote(
    *,
    partner_user,
    shop: Shop,
    client_user=None,
    client_name: str,
    client_email: str = "",
    client_phone: str = "",
    calculator_inputs_snapshot: dict[str, Any],
    pricing_snapshot: dict[str, Any],
    partner_markup: Decimal,
    title: str = "",
    note: str = "",
) -> dict[str, Any]:
    validate_partner_markup(pricing_snapshot=pricing_snapshot, shop=shop, partner_markup=partner_markup)
    preview = build_partner_quote_preview(
        pricing_snapshot=pricing_snapshot,
        shop=shop,
        partner_markup=partner_markup,
    )
    final_client_price = _money(preview.get("client_price"))
    production_base_price = _money(preview.get("production_estimate"))
    split = calculate_partner_job_split(
        production_base_price,
        broker_margin_percent=(partner_markup / production_base_price * Decimal("100")) if production_base_price > 0 else Decimal("0"),
        platform_service_percent=get_default_platform_service_percent(),
        partner_user=partner_user,
    )
    partner_brand_name = getattr(partner_user, "name", "") or getattr(partner_user, "email", "") or "Partner"

    draft = save_quote_draft(
        user=partner_user,
        shop=shop,
        title=title or f"Partner quote for {client_name or 'client'}",
        calculator_inputs_snapshot=calculator_inputs_snapshot,
        pricing_snapshot=pricing_snapshot,
        request_details_snapshot={
            "customer_name": client_name,
            "customer_email": client_email,
            "customer_phone": client_phone,
            "selected_shop_ids": [shop.id],
            "quote_source": "partner_quote_builder",
            "white_label_mode": True,
            "partner_brand_name": partner_brand_name,
            "partner_markup": str(partner_markup.quantize(Decimal("0.01"))),
        },
    )
    quote_request = send_quote_draft_to_shops(
        draft=draft,
        shops=[shop],
        request_details_snapshot={
            "client_id": getattr(client_user, "id", None),
            "customer_name": client_name,
            "customer_email": client_email,
            "customer_phone": client_phone,
            "quote_source": "partner_quote_builder",
            "white_label_mode": True,
            "partner_brand_name": partner_brand_name,
        },
    )[0]

    customer = get_or_create_partner_customer(
        shop=shop,
        partner_user=partner_user,
        client_name=client_name,
        client_email=client_email,
        client_phone=client_phone,
    )
    request_snapshot = _as_dict(quote_request.request_snapshot)
    request_snapshot.update(
        {
            "quote_source": "partner_quote_builder",
            "partner_brand_name": partner_brand_name,
            "white_label_mode": True,
            "partner_markup": str(partner_markup.quantize(Decimal("0.01"))),
            "relationship_owner_type": "user",
            "relationship_owner_user_id": partner_user.id,
            "topology_mode": TOPOLOGY_MANAGED,
        }
    )
    request_snapshot["visibility"] = {
        "actor": "client",
        "topology_mode": TOPOLOGY_MANAGED,
        "exposes_internal_economics": False,
    }
    quote_request.customer = customer
    quote_request.on_behalf_of = client_user
    quote_request.request_snapshot = request_snapshot
    quote_request.save(update_fields=["customer", "on_behalf_of", "request_snapshot", "updated_at"])

    response_snapshot = {
        "currency": pricing_snapshot.get("currency") or "KES",
        "partner_brand_name": partner_brand_name,
        "white_label_mode": True,
        "customer_pricing": {
            "production_base_price": str(split["production_amount"]),
            "broker_margin_percent": str(split["broker_margin_percent"]),
            "broker_margin_amount": str(split["broker_margin_amount"]),
            "platform_service_percent": str(split["platform_service_percent"]),
            "platform_service_amount": str(split["platform_service_amount"]),
            "final_client_price": str(split["client_total"]),
        },
        "pricing": {
            "grand_total": str(split["client_total"]),
        },
        "totals": {
            "grand_total": str(split["client_total"]),
        },
        "payment_terms": "Pay through Printy before production starts.",
        "note": note or "Partner quote prepared in Printy.",
    }
    response = create_quote_response(
        quote_request=quote_request,
        shop=shop,
        user=partner_user,
        status="sent",
        response_snapshot=response_snapshot,
        revised_pricing_snapshot=None,
        total=production_base_price,
        note=note or "Partner quote prepared in Printy.",
    )
    response.production_base_price = split["production_amount"]
    response.broker_margin_type = "fixed"
    response.broker_margin_value = partner_markup.quantize(Decimal("0.01"))
    response.broker_margin_amount = split["broker_margin_amount"]
    response.platform_service_percent = split["platform_service_percent"]
    response.platform_service_amount = split["platform_service_amount"]
    response.client_total = split["client_total"]
    response.sent_to_client_at = response.sent_at
    response.sent_to_client_by = partner_user
    response.client_quote_status = "sent"
    response.save(
        update_fields=[
            "production_base_price",
            "broker_margin_type",
            "broker_margin_value",
            "broker_margin_amount",
            "platform_service_percent",
            "platform_service_amount",
            "client_total",
            "sent_to_client_at",
            "sent_to_client_by",
            "client_quote_status",
            "updated_at",
        ]
    )
    return {
        "draft": draft,
        "quote_request": quote_request,
        "shop_quote": response,
        "preview": preview,
    }
