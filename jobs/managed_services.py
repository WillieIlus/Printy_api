"""ManagedJob creation services for additive quote-to-job orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from api.visibility import (
    TOPOLOGY_MANAGED,
    resolve_topology_mode_for_quote_request,
)
from jobs.audit_services import (
    EVENT_ASSIGNMENT_CREATED,
    EVENT_MANAGED_JOB_CREATED,
    EVENT_QUOTE_ACCEPTED,
    record_managed_job_event,
)
from jobs.file_services import import_legacy_files_to_managed_job
from jobs.choices import ManagedJobTopologyType
from jobs.models import JobAssignment, ManagedJob
from jobs.workflow import assignment_status_from_production_order, managed_status_from_shop_quote_status
from production.models import Customer, ProductionOrder
from quotes.models import QuoteDraft, QuoteRequest, ShopQuote
from services.pricing.urgency import determine_operational_priority, normalize_urgency_type


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _resolve_source_draft(quote_request: QuoteRequest | None) -> QuoteDraft | None:
    if not quote_request:
        return None
    return getattr(quote_request, "source_draft", None)


def _resolve_client(quote_request: QuoteRequest | None):
    if not quote_request:
        return None
    return getattr(quote_request, "created_by", None)


def _resolve_customer(quote_request: QuoteRequest | None) -> Customer | None:
    if not quote_request:
        return None
    customer = getattr(quote_request, "customer", None)
    if customer:
        return customer
    return None


def _resolve_relationship_snapshot(customer: Customer | None) -> dict[str, Any]:
    if not customer:
        return {}
    return {
        "owner_type": customer.relationship_owner_type,
        "owner_reference": customer.relationship_owner_reference(),
        "owner_user_id": customer.relationship_owner_user_id,
        "owner_shop_id": customer.relationship_owner_shop_id,
        "acquisition_source": customer.acquisition_source,
    }


def _resolve_broker(customer: Customer | None):
    if not customer:
        return None
    if customer.relationship_owner_type == Customer.RelationshipOwnerType.USER:
        return customer.relationship_owner_user
    return None


def _resolve_fulfillment_mode(quote_request: QuoteRequest | None) -> str:
    request_snapshot = _as_dict(getattr(quote_request, "request_snapshot", None))
    request_details = _as_dict(request_snapshot.get("request_details"))
    delivery_preference = (
        request_details.get("delivery_preference")
        or getattr(quote_request, "delivery_preference", "")
        or ""
    ).strip().lower()
    if delivery_preference == "delivery":
        return "printy_rider"
    return "pickup"


def _resolve_topology_type(customer: Customer | None) -> str:
    if customer and customer.relationship_owner_type == Customer.RelationshipOwnerType.USER:
        return ManagedJobTopologyType.CLIENT_PARTNER
    return ManagedJobTopologyType.CLIENT_PRINTY_SUPPORT


def _resolve_urgency_payload(*, quote_request: QuoteRequest | None, shop_quote: ShopQuote | None) -> dict[str, Any]:
    response_snapshot = _as_dict(getattr(shop_quote, "response_snapshot", None))
    revised_snapshot = _as_dict(getattr(shop_quote, "revised_pricing_snapshot", None))
    request_snapshot = _as_dict(getattr(quote_request, "request_snapshot", None))
    request_details = _as_dict(request_snapshot.get("request_details"))

    turnaround_hours = getattr(shop_quote, "turnaround_hours", None)
    turnaround_label = getattr(shop_quote, "turnaround_label", "") or response_snapshot.get("turnaround_label")
    urgency_type = normalize_urgency_type(
        response_snapshot.get("urgency_type") or revised_snapshot.get("urgency_type") or request_details.get("urgency_type"),
        turnaround_hours=turnaround_hours,
        turnaround_label=turnaround_label,
    )
    priority_level = determine_operational_priority(
        urgency_type=urgency_type,
        turnaround_hours=turnaround_hours,
        turnaround_label=turnaround_label,
    )

    def _coerce_datetime(value: Any):
        if not value:
            return None
        if hasattr(value, "tzinfo"):
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            return parsed
        return None

    return {
        "urgency_type": urgency_type,
        "urgency_multiplier": response_snapshot.get("urgency_multiplier") or revised_snapshot.get("urgency_multiplier"),
        "urgency_fee": response_snapshot.get("urgency_fee") or revised_snapshot.get("urgency_fee"),
        "after_hours_fee": response_snapshot.get("after_hours_fee") or revised_snapshot.get("after_hours_fee"),
        "requested_deadline": _coerce_datetime(request_details.get("requested_deadline")),
        "requested_delivery_time": _coerce_datetime(request_details.get("requested_delivery_time")),
        "operational_priority_level": priority_level,
    }


def _build_commercial_snapshot(*, quote_request: QuoteRequest, shop_quote: ShopQuote, source_draft: QuoteDraft | None) -> dict[str, Any]:
    request_snapshot = _as_dict(getattr(quote_request, "request_snapshot", None))
    return {
        "quote_request_id": quote_request.id,
        "quote_request_reference": quote_request.request_reference,
        "shop_quote_id": shop_quote.id,
        "shop_quote_reference": shop_quote.quote_reference,
        "quote_status": quote_request.status,
        "shop_quote_status": shop_quote.status,
        "currency": getattr(shop_quote.shop, "currency", "KES") or "KES",
        "client_total": str(shop_quote.total) if shop_quote.total is not None else None,
        "response_snapshot": _as_dict(shop_quote.response_snapshot),
        "revised_pricing_snapshot": _as_dict(shop_quote.revised_pricing_snapshot),
        "request_customer_pricing": _as_dict(request_snapshot.get("customer_pricing")),
        "source_draft_reference": getattr(source_draft, "draft_reference", ""),
        "visibility": {
            "topology_mode": TOPOLOGY_MANAGED,
            "exposes_internal_economics": False,
        },
    }


def _build_operational_snapshot(*, quote_request: QuoteRequest, shop_quote: ShopQuote, source_draft: QuoteDraft | None) -> dict[str, Any]:
    request_snapshot = _as_dict(getattr(quote_request, "request_snapshot", None))
    urgency_payload = _resolve_urgency_payload(quote_request=quote_request, shop_quote=shop_quote)
    return {
        "quote_request_id": quote_request.id,
        "shop_id": shop_quote.shop_id,
        "shop_slug": getattr(shop_quote.shop, "slug", ""),
        "selected_shop": _as_dict(request_snapshot.get("selected_shop")),
        "selected_shop_preview": _as_dict(request_snapshot.get("selected_shop_preview")),
        "matched_specs": request_snapshot.get("matched_specs") or [],
        "needs_confirmation": request_snapshot.get("needs_confirmation") or [],
        "delivery_preference": getattr(quote_request, "delivery_preference", ""),
        "delivery_address": getattr(quote_request, "delivery_address", ""),
        "delivery_location_id": getattr(quote_request, "delivery_location_id", None),
        "urgency_type": urgency_payload["urgency_type"],
        "urgency_multiplier": urgency_payload["urgency_multiplier"],
        "urgency_fee": urgency_payload["urgency_fee"],
        "after_hours_fee": urgency_payload["after_hours_fee"],
        "requested_deadline": urgency_payload["requested_deadline"].isoformat() if urgency_payload["requested_deadline"] else None,
        "requested_delivery_time": urgency_payload["requested_delivery_time"].isoformat() if urgency_payload["requested_delivery_time"] else None,
        "operational_priority_level": urgency_payload["operational_priority_level"],
        "source_draft_reference": getattr(source_draft, "draft_reference", ""),
        "topology_mode": resolve_topology_mode_for_quote_request(quote_request),
    }


def _build_assignment_snapshot(*, managed_job: ManagedJob, shop_quote: ShopQuote) -> dict[str, Any]:
    return {
        "managed_job_id": managed_job.id,
        "managed_reference": managed_job.managed_reference,
        "source_shop_quote_id": shop_quote.id,
        "shop_id": shop_quote.shop_id,
        "shop_slug": getattr(shop_quote.shop, "slug", ""),
        "topology_type": managed_job.topology_type,
        "fulfillment_mode": managed_job.fulfillment_mode,
        "urgency_type": managed_job.urgency_type,
        "operational_priority_level": managed_job.operational_priority_level,
        "requested_deadline": managed_job.requested_deadline.isoformat() if managed_job.requested_deadline else None,
    }


@transaction.atomic
def create_managed_job_from_accepted_quote(
    *,
    quote_request: QuoteRequest,
    shop_quote: ShopQuote,
    accepted_by=None,
) -> ManagedJob:
    managed_job = (
        ManagedJob.objects.select_related("source_production_order")
        .filter(source_shop_quote=shop_quote)
        .first()
    )
    if managed_job:
        import_legacy_files_to_managed_job(
            managed_job=managed_job,
            quote_request=quote_request,
            shop_quote=shop_quote,
        )
        return managed_job

    source_draft = _resolve_source_draft(quote_request)
    customer = _resolve_customer(quote_request)
    broker = _resolve_broker(customer)
    topology_mode = resolve_topology_mode_for_quote_request(quote_request)
    urgency_payload = _resolve_urgency_payload(quote_request=quote_request, shop_quote=shop_quote)

    managed_job = ManagedJob.objects.create(
        title=shop_quote.note[:255] if shop_quote.note else (quote_request.notes[:255] if quote_request.notes else f"Managed job from quote {shop_quote.quote_reference or shop_quote.id}"),
        source_quote_request=quote_request,
        source_shop_quote=shop_quote,
        client=_resolve_client(quote_request),
        customer=customer,
        broker=broker,
        assigned_shop=shop_quote.shop,
        created_by=accepted_by or _resolve_client(quote_request) or shop_quote.created_by,
        status=managed_status_from_shop_quote_status(shop_quote.status),
        payment_status="pending",
        assignment_status="unassigned",
        exception_status="clear",
        fulfillment_mode=_resolve_fulfillment_mode(quote_request),
        topology_type=_resolve_topology_type(customer),
        urgency_type=urgency_payload["urgency_type"],
        urgency_multiplier=urgency_payload["urgency_multiplier"],
        urgency_fee=urgency_payload["urgency_fee"],
        after_hours_fee=urgency_payload["after_hours_fee"],
        requested_deadline=urgency_payload["requested_deadline"],
        requested_delivery_time=urgency_payload["requested_delivery_time"],
        operational_priority_level=urgency_payload["operational_priority_level"],
        client_total=shop_quote.total,
        commercial_snapshot=_build_commercial_snapshot(
            quote_request=quote_request,
            shop_quote=shop_quote,
            source_draft=source_draft,
        ),
        operational_snapshot=_build_operational_snapshot(
            quote_request=quote_request,
            shop_quote=shop_quote,
            source_draft=source_draft,
        ),
        workflow_metadata={
            "created_from": "accepted_quote",
            "accepted_via_quote_request_id": quote_request.id,
            "accepted_via_shop_quote_id": shop_quote.id,
            "topology_mode": topology_mode,
        },
        relationship_snapshot=_resolve_relationship_snapshot(customer),
        accepted_at=shop_quote.accepted_at or timezone.now(),
    )
    import_legacy_files_to_managed_job(
        managed_job=managed_job,
        quote_request=quote_request,
        shop_quote=shop_quote,
    )
    record_managed_job_event(
        managed_job=managed_job,
        actor=accepted_by or managed_job.created_by,
        event_type=EVENT_QUOTE_ACCEPTED,
        summary="Accepted quote linked to managed job.",
        metadata={
            "quote_request_id": quote_request.id,
            "shop_quote_id": shop_quote.id,
        },
    )
    record_managed_job_event(
        managed_job=managed_job,
        actor=accepted_by or managed_job.created_by,
        event_type=EVENT_MANAGED_JOB_CREATED,
        summary="Managed job created from accepted quote.",
        metadata={
            "quote_request_id": quote_request.id,
            "shop_quote_id": shop_quote.id,
            "topology_mode": topology_mode,
        },
    )
    return managed_job


@transaction.atomic
def create_assignment_for_managed_job(
    *,
    managed_job: ManagedJob,
    shop_quote: ShopQuote | None = None,
) -> JobAssignment:
    assignment = (
        JobAssignment.objects.select_related("production_order")
        .filter(managed_job=managed_job, reassigned_from__isnull=True)
        .first()
    )
    if assignment:
        import_legacy_files_to_managed_job(
            managed_job=managed_job,
            quote_request=managed_job.source_quote_request,
            shop_quote=shop_quote or managed_job.source_shop_quote,
        )
        return assignment

    source_shop_quote = shop_quote or managed_job.source_shop_quote
    assigned_shop = managed_job.assigned_shop or getattr(source_shop_quote, "shop", None)

    assignment = JobAssignment.objects.create(
        managed_job=managed_job,
        assigned_shop=assigned_shop,
        source_shop_quote=source_shop_quote,
        status="pending",
        production_amount=managed_job.production_total,
        urgency_type=managed_job.urgency_type,
        operational_priority_level=managed_job.operational_priority_level,
        assignment_notes="Initial assignment created from accepted quote.",
        requested_deadline=managed_job.requested_deadline,
        operational_snapshot=_build_assignment_snapshot(
            managed_job=managed_job,
            shop_quote=source_shop_quote,
        ) if source_shop_quote else {
            "managed_job_id": managed_job.id,
            "managed_reference": managed_job.managed_reference,
        },
    )
    import_legacy_files_to_managed_job(
        managed_job=managed_job,
        quote_request=managed_job.source_quote_request,
        shop_quote=source_shop_quote,
    )
    record_managed_job_event(
        managed_job=managed_job,
        assignment=assignment,
        actor=managed_job.created_by,
        event_type=EVENT_ASSIGNMENT_CREATED,
        summary="Assignment created for managed job.",
        metadata={
            "assigned_shop_id": assigned_shop.id if assigned_shop else None,
            "source_shop_quote_id": source_shop_quote.id if source_shop_quote else None,
        },
    )
    return assignment


@transaction.atomic
def attach_production_order_to_managed_job(*, managed_job: ManagedJob, production_order: ProductionOrder) -> ManagedJob:
    if managed_job.source_production_order_id != production_order.id:
        managed_job.source_production_order = production_order
        managed_job.operational_snapshot = {
            **_as_dict(managed_job.operational_snapshot),
            "production_order_id": production_order.id,
            "production_order_status": production_order.status,
            "production_delivery_status": production_order.delivery_status,
        }
        managed_job.save(update_fields=["source_production_order", "operational_snapshot", "updated_at"])
    return managed_job


@transaction.atomic
def attach_production_order_to_assignment(*, assignment: JobAssignment, production_order: ProductionOrder) -> JobAssignment:
    next_status = assignment_status_from_production_order(
        status=production_order.status,
        delivery_status=production_order.delivery_status,
    )
    update_fields: list[str] = ["updated_at"]

    if assignment.production_order_id != production_order.id:
        assignment.production_order = production_order
        update_fields.append("production_order")

    if assignment.status != next_status:
        assignment.status = next_status
        update_fields.append("status")

    assignment.operational_snapshot = {
        **_as_dict(assignment.operational_snapshot),
        "production_order_id": production_order.id,
        "production_order_status": production_order.status,
        "production_delivery_status": production_order.delivery_status,
    }
    update_fields.append("operational_snapshot")
    assignment.save(update_fields=update_fields)
    return assignment
