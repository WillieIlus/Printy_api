"""Partner quote builder helpers on top of the existing quote workflow."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from api.visibility import TOPOLOGY_MANAGED
from jobs.payment_services import calculate_partner_job_split, get_default_platform_service_percent
from production.models import Customer
from quotes.choices import QuoteStatus, ShopQuoteStatus
from quotes.guardrails import build_partner_markup_warning, calculate_quote_expiry, validate_partner_markup_amount
from quotes.models import QuoteRequest, ShopQuote
from quotes.services_workflow import _build_reference, create_quote_response
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
            "markup_warning": build_partner_markup_warning(
                base_price=production_estimate,
                markup_amount=partner_markup,
            ),
        }
    )
    return broker_projection


def validate_partner_markup(*, pricing_snapshot: dict[str, Any], shop: Shop, partner_markup: Decimal) -> None:
    preview = build_partner_quote_preview(pricing_snapshot=pricing_snapshot, shop=shop, partner_markup=partner_markup)
    production_estimate = _money(preview.get("production_estimate"))
    validate_partner_markup_amount(base_price=production_estimate, markup_amount=partner_markup)


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


def _build_partner_request_snapshot(
    *,
    shop: Shop,
    calculator_inputs_snapshot: dict[str, Any],
    pricing_snapshot: dict[str, Any],
    partner_user,
    partner_brand_name: str,
    partner_markup: Decimal,
    client_name: str,
    client_email: str,
    client_phone: str,
    client_company: str,
    client_user=None,
) -> dict[str, Any]:
    return {
        "source": "partner_quote_builder",
        "quote_source": "partner_quote_builder",
        "calculator_inputs": calculator_inputs_snapshot,
        "pricing_snapshot": pricing_snapshot,
        "request_details": {
            "customer_name": client_name,
            "customer_email": client_email,
            "customer_phone": client_phone,
            "client_company": client_company,
        },
        "selected_shop_ids": [shop.id],
        "selected_shop_preview": {
            "id": shop.id,
            "slug": shop.slug,
            "name": shop.name,
        },
        "partner_brand_name": partner_brand_name,
        "white_label_mode": True,
        "partner_markup": str(partner_markup.quantize(Decimal("0.01"))),
        "relationship_owner_type": "user",
        "relationship_owner_user_id": partner_user.id,
        "topology_mode": TOPOLOGY_MANAGED,
        "visibility": {
            "actor": "client",
            "topology_mode": TOPOLOGY_MANAGED,
            "exposes_internal_economics": False,
        },
        "pending_client": {
            "client_user_id": getattr(client_user, "id", None),
            "name": client_name,
            "email": client_email,
            "phone": client_phone,
            "company": client_company,
        },
    }


@transaction.atomic
def create_partner_quote(
    *,
    partner_user,
    shop: Shop,
    client_user=None,
    client_name: str,
    client_email: str = "",
    client_phone: str = "",
    client_company: str = "",
    calculator_inputs_snapshot: dict[str, Any],
    pricing_snapshot: dict[str, Any],
    partner_markup: Decimal,
    title: str = "",
    note: str = "",
    save_as_draft: bool = False,
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
    customer = None
    if client_name or client_email or client_phone:
        customer = get_or_create_partner_customer(
            shop=shop,
            partner_user=partner_user,
            client_name=client_name or client_email or client_phone,
            client_email=client_email,
            client_phone=client_phone,
        )
    quote_request = QuoteRequest.objects.create(
        shop=shop,
        created_by=partner_user,
        on_behalf_of=client_user if not save_as_draft else None,
        customer=customer,
        customer_name=client_name or client_email or client_phone or "Client",
        customer_email=client_email,
        customer_phone=client_phone,
        notes=note or "Partner quote prepared in Printy.",
        status=QuoteStatus.DRAFT if save_as_draft else QuoteStatus.QUOTED,
        request_snapshot=_build_partner_request_snapshot(
            shop=shop,
            calculator_inputs_snapshot=calculator_inputs_snapshot,
            pricing_snapshot=pricing_snapshot,
            partner_user=partner_user,
            partner_brand_name=partner_brand_name,
            partner_markup=partner_markup,
            client_name=client_name,
            client_email=client_email,
            client_phone=client_phone,
            client_company=client_company,
            client_user=client_user,
        ),
    )
    quote_request.request_reference = _build_reference("QR", quote_request.id)
    quote_request.save(update_fields=["request_reference", "updated_at"])

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
    if save_as_draft:
        response = ShopQuote.objects.create(
            quote_request=quote_request,
            shop=shop,
            created_by=partner_user,
            status=ShopQuoteStatus.PENDING,
            total=production_base_price,
            note=note or "Partner quote draft prepared in Printy.",
            response_snapshot=response_snapshot,
            revised_pricing_snapshot=None,
        )
        response.quote_reference = _build_reference("QS", response.id)
        response.production_base_price = split["production_amount"]
        response.broker_margin_type = "fixed"
        response.broker_margin_value = partner_markup.quantize(Decimal("0.01"))
        response.broker_margin_amount = split["broker_margin_amount"]
        response.platform_service_percent = split["platform_service_percent"]
        response.platform_service_amount = split["platform_service_amount"]
        response.client_total = split["client_total"]
        response.client_quote_status = "draft"
        response.save(
            update_fields=[
                "quote_reference",
                "production_base_price",
                "broker_margin_type",
                "broker_margin_value",
                "broker_margin_amount",
                "platform_service_percent",
                "platform_service_amount",
                "client_total",
                "client_quote_status",
                "updated_at",
            ]
        )
    else:
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
    if not save_as_draft:
        sent_at = response.sent_at or timezone.now()
        response.sent_at = sent_at
        response.expires_at = calculate_quote_expiry(sent_at=sent_at)
    response.production_base_price = split["production_amount"]
    response.broker_margin_type = "fixed"
    response.broker_margin_value = partner_markup.quantize(Decimal("0.01"))
    response.broker_margin_amount = split["broker_margin_amount"]
    response.platform_service_percent = split["platform_service_percent"]
    response.platform_service_amount = split["platform_service_amount"]
    response.client_total = split["client_total"]
    response.sent_to_client_at = response.sent_at if not save_as_draft else None
    response.sent_to_client_by = partner_user if not save_as_draft else None
    response.client_quote_status = "draft" if save_as_draft else "sent"
    response.save(
        update_fields=[
            "sent_at",
            "expires_at",
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
        "draft": None,
        "quote_request": quote_request,
        "shop_quote": response,
        "preview": preview,
    }


@transaction.atomic
def respond_to_assigned_quote_request(
    *,
    partner_user,
    quote_request,
    shop: Shop,
    pricing_snapshot: dict[str, Any],
    partner_markup: Decimal,
    note: str = "",
) -> dict[str, Any]:
    validate_partner_markup(pricing_snapshot=pricing_snapshot, shop=shop, partner_markup=partner_markup)
    preview = build_partner_quote_preview(
        pricing_snapshot=pricing_snapshot,
        shop=shop,
        partner_markup=partner_markup,
    )
    production_base_price = _money(preview.get("production_estimate"))
    split = calculate_partner_job_split(
        production_base_price,
        broker_margin_percent=(partner_markup / production_base_price * Decimal("100")) if production_base_price > 0 else Decimal("0"),
        platform_service_percent=get_default_platform_service_percent(),
        partner_user=partner_user,
    )
    partner_brand_name = getattr(partner_user, "name", "") or getattr(partner_user, "email", "") or "Print Manager"

    request_snapshot = _as_dict(quote_request.request_snapshot)
    request_snapshot.update(
        {
            "partner_brand_name": partner_brand_name,
            "relationship_owner_type": "user",
            "relationship_owner_user_id": partner_user.id,
            "selected_shop_ids": [shop.id],
            "selected_shop_preview": {
                "id": shop.id,
                "slug": shop.slug,
                "name": shop.name,
            },
            "topology_mode": TOPOLOGY_MANAGED,
        }
    )
    request_snapshot["visibility"] = {
        "actor": "client",
        "topology_mode": TOPOLOGY_MANAGED,
        "exposes_internal_economics": False,
    }
    quote_request.request_snapshot = request_snapshot
    quote_request.save(update_fields=["request_snapshot", "updated_at"])

    response_snapshot = {
        "currency": pricing_snapshot.get("currency") or "KES",
        "partner_brand_name": partner_brand_name,
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
        "note": note or "Your Print Manager prepared an exact quote in Printy.",
    }
    response = create_quote_response(
        quote_request=quote_request,
        shop=shop,
        user=partner_user,
        status="sent",
        response_snapshot=response_snapshot,
        revised_pricing_snapshot=None,
        total=production_base_price,
        note=note or "Your Print Manager prepared an exact quote in Printy.",
    )
    sent_at = response.sent_at or timezone.now()
    response.sent_at = sent_at
    response.expires_at = calculate_quote_expiry(sent_at=sent_at)
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
            "sent_at",
            "expires_at",
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
        "quote_request": quote_request,
        "shop_quote": response,
        "preview": preview,
    }
