"""Managed job payment confirmation and settlement helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing.services.payments import (
    _build_stk_password,
    _get_mpesa_config,
    _get_mpesa_timeout,
    _safe_json_response,
    get_mpesa_token,
    normalize_phone_number,
    parse_callback,
    query_transaction_status,
    verify_callback_minimally,
)
from jobs.audit_services import (
    EVENT_PAYMENT_CONFIRMED,
    EVENT_SETTLEMENT_RELEASE_READY,
    record_managed_job_event,
)
from jobs.choices import (
    JobPaymentChannel,
    JobPaymentMethod,
    JobPaymentReconciliationStatus,
    JobPaymentStatus,
    JobSettlementStatus,
    ManagedJobPaymentStatus,
    ManagedJobStatus,
)
from jobs.models import JobPayment, JobSettlementSplit, ManagedJob
import requests


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _money(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _string(value: Any, default: str = "") -> str:
    if value in (None, ""):
        return default
    return str(value)


def generate_job_account_reference(*, managed_job: ManagedJob) -> str:
    reference = (managed_job.managed_reference or f"MJ-{managed_job.id or 'NEW'}").upper()
    cleaned = "".join(ch for ch in reference if ch.isalnum() or ch == "-")
    return cleaned[:20]


def _build_job_transaction_description(managed_job: ManagedJob) -> str:
    prefix = str(
        getattr(settings, "MPESA_TRANSACTION_DESC_DEFAULT", "Printy payment") or "Printy payment"
    ).strip()
    return f"{prefix} {generate_job_account_reference(managed_job=managed_job)}"[:255].strip()


def _infer_channel(payment_method: str) -> str:
    if payment_method == JobPaymentMethod.CASH:
        return JobPaymentChannel.CASH
    if payment_method == JobPaymentMethod.MANUAL:
        return JobPaymentChannel.MANUAL
    return JobPaymentChannel.STK_PUSH


def _map_job_result_code_to_status(result_code: str) -> str:
    if result_code == "0":
        return JobPaymentStatus.CONFIRMED
    if result_code in {"1037", "1019", "1025"}:
        return JobPaymentStatus.FAILED
    if result_code in {"1032"}:
        return JobPaymentStatus.FAILED
    return JobPaymentStatus.FAILED


def _parse_c2b_callback(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    trans_id = _string(payload.get("TransID"))
    bill_ref = _string(payload.get("BillRefNumber"))
    if not trans_id or not bill_ref:
        return {}
    try:
        amount = Decimal(str(payload.get("TransAmount")))
    except Exception:
        amount = None
    return {
        "channel": JobPaymentChannel.PAYBILL_MANUAL,
        "mpesa_receipt_number": trans_id,
        "account_reference": bill_ref,
        "payer_phone": _string(payload.get("MSISDN")),
        "received_amount": amount,
        "transaction_date": _string(payload.get("TransTime")),
        "raw_payload": payload,
    }


def _find_duplicate_receipt(*, payment: JobPayment, receipt: str) -> JobPayment | None:
    if not receipt:
        return None
    return JobPayment.objects.select_for_update().filter(
        mpesa_receipt_number=receipt
    ).exclude(id=payment.id).first()


def _relationship_snapshot(managed_job: ManagedJob) -> dict[str, Any]:
    snapshot = _as_dict(getattr(managed_job, "relationship_snapshot", None))
    owner_type = snapshot.get("owner_type") or "printy"
    if owner_type == "unknown":
        owner_type = "printy"
    if owner_type == "client":
        owner_type = "printy"
    return {
        "owner_type": owner_type,
        "owner_reference": snapshot.get("owner_reference") or ("printy" if owner_type == "printy" else ""),
        "owner_user_id": snapshot.get("owner_user_id"),
        "owner_shop_id": snapshot.get("owner_shop_id"),
    }


def _extract_delivery_amount(managed_job: ManagedJob) -> Decimal:
    commercial = _as_dict(getattr(managed_job, "commercial_snapshot", None))
    request_pricing = _as_dict(commercial.get("request_customer_pricing"))
    pricing_summary = _as_dict(request_pricing.get("pricing_summary"))
    for line in pricing_summary.get("lines") or []:
        if not isinstance(line, dict):
            continue
        label = str(line.get("label") or "").strip().lower()
        if label == "delivery":
            return _money(line.get("amount"))
    response_snapshot = _as_dict(commercial.get("response_snapshot"))
    pricing = _as_dict(response_snapshot.get("pricing"))
    totals = _as_dict(response_snapshot.get("totals"))
    return _money(totals.get("delivery_fee") or pricing.get("delivery_fee"))


def _extract_production_amount(managed_job: ManagedJob) -> Decimal:
    if managed_job.production_total is not None:
        return _money(managed_job.production_total)
    commercial = _as_dict(getattr(managed_job, "commercial_snapshot", None))
    response_snapshot = _as_dict(commercial.get("response_snapshot"))
    revised_snapshot = _as_dict(commercial.get("revised_pricing_snapshot"))
    return (
        _money(_as_dict(response_snapshot.get("totals")).get("subtotal"))
        or _money(_as_dict(response_snapshot.get("pricing")).get("subtotal"))
        or _money(_as_dict(revised_snapshot.get("totals")).get("subtotal"))
        or _money(managed_job.client_total)
    )


def _extract_urgency_amount(managed_job: ManagedJob) -> Decimal:
    operational = _as_dict(getattr(managed_job, "operational_snapshot", None))
    commercial = _as_dict(getattr(managed_job, "commercial_snapshot", None))
    response_snapshot = _as_dict(commercial.get("response_snapshot"))
    revised_snapshot = _as_dict(commercial.get("revised_pricing_snapshot"))
    urgency_fee = (
        _money(managed_job.urgency_fee)
        or _money(operational.get("urgency_fee"))
        or _money(response_snapshot.get("urgency_fee"))
        or _money(revised_snapshot.get("urgency_fee"))
    )
    after_hours_fee = (
        _money(managed_job.after_hours_fee)
        or _money(operational.get("after_hours_fee"))
        or _money(response_snapshot.get("after_hours_fee"))
        or _money(revised_snapshot.get("after_hours_fee"))
    )
    return urgency_fee + after_hours_fee


def _allocate_urgency_premium(*, managed_job: ManagedJob, urgency_total: Decimal) -> dict[str, Decimal]:
    if urgency_total <= 0:
        return {
            "production_bonus": Decimal("0"),
            "partner_bonus": Decimal("0"),
            "platform_bonus": Decimal("0"),
        }

    production_bonus = (urgency_total * Decimal("0.70")).quantize(Decimal("0.01"))
    partner_bonus = (urgency_total * Decimal("0.15")).quantize(Decimal("0.01"))
    platform_bonus = urgency_total - production_bonus - partner_bonus

    owner_type = _relationship_snapshot(managed_job)["owner_type"]
    if owner_type not in {"user", "shop"}:
        platform_bonus += partner_bonus
        partner_bonus = Decimal("0")

    return {
        "production_bonus": production_bonus,
        "partner_bonus": partner_bonus,
        "platform_bonus": platform_bonus,
    }


def calculate_settlement_split(*, managed_job: ManagedJob, payment_method: str | None = None) -> dict[str, Any]:
    client_total = _money(managed_job.client_total)
    production_amount = _extract_production_amount(managed_job)
    delivery_amount = _extract_delivery_amount(managed_job)
    partner_commission = _money(managed_job.broker_commission)
    platform_fee = _money(managed_job.platform_fee)
    urgency_total = _extract_urgency_amount(managed_job)
    urgency_allocations = _allocate_urgency_premium(managed_job=managed_job, urgency_total=urgency_total)

    production_amount += urgency_allocations["production_bonus"]
    partner_commission += urgency_allocations["partner_bonus"]
    platform_fee += urgency_allocations["platform_bonus"]

    if platform_fee == 0:
        residual = client_total - production_amount - delivery_amount - partner_commission
        if residual > 0:
            platform_fee = residual

    snapshot = _relationship_snapshot(managed_job)
    owner_type = snapshot["owner_type"]
    recipient_type = owner_type if owner_type in {"printy", "user", "shop"} else "printy"

    return {
        "production_amount": production_amount,
        "platform_fee": platform_fee,
        "partner_commission": partner_commission,
        "delivery_amount": delivery_amount,
        "client_total": client_total,
        "relationship_owner_type": owner_type,
        "relationship_owner_user_id": snapshot.get("owner_user_id"),
        "relationship_owner_shop_id": snapshot.get("owner_shop_id"),
        "relationship_owner_reference": snapshot.get("owner_reference") or "",
        "commission_recipient_type": recipient_type,
        "payment_method": payment_method or JobPaymentMethod.MPESA,
    }


@transaction.atomic
def initialize_settlement_for_managed_job(
    *,
    managed_job: ManagedJob,
    payment_method: str | None = None,
) -> JobSettlementSplit:
    payload = calculate_settlement_split(managed_job=managed_job, payment_method=payment_method)
    settlement, created = JobSettlementSplit.objects.get_or_create(
        managed_job=managed_job,
        defaults={
            "production_amount": payload["production_amount"],
            "platform_fee": payload["platform_fee"],
            "partner_commission": payload["partner_commission"],
            "delivery_amount": payload["delivery_amount"],
            "client_total": payload["client_total"],
            "relationship_owner_type": payload["relationship_owner_type"],
            "relationship_owner_user_id": payload["relationship_owner_user_id"],
            "relationship_owner_shop_id": payload["relationship_owner_shop_id"],
            "relationship_owner_reference": payload["relationship_owner_reference"],
            "commission_recipient_type": payload["commission_recipient_type"],
            "payment_method": payload["payment_method"],
        },
    )
    if not created:
        changed = False
        for field in (
            "production_amount",
            "platform_fee",
            "partner_commission",
            "delivery_amount",
            "client_total",
            "relationship_owner_type",
            "relationship_owner_reference",
            "commission_recipient_type",
            "payment_method",
        ):
            new_value = payload[field]
            if getattr(settlement, field) != new_value:
                setattr(settlement, field, new_value)
                changed = True
        if settlement.relationship_owner_user_id != payload["relationship_owner_user_id"]:
            settlement.relationship_owner_user_id = payload["relationship_owner_user_id"]
            changed = True
        if settlement.relationship_owner_shop_id != payload["relationship_owner_shop_id"]:
            settlement.relationship_owner_shop_id = payload["relationship_owner_shop_id"]
            changed = True
        if changed:
            settlement.save()
    return settlement


@transaction.atomic
def create_job_payment(
    *,
    managed_job: ManagedJob,
    payer=None,
    amount: Decimal | None = None,
    payment_method: str = JobPaymentMethod.MPESA,
    payment_channel: str | None = None,
    external_reference: str = "",
    account_reference: str = "",
    payer_phone: str = "",
    expected_amount: Decimal | None = None,
    raw_gateway_payload: dict[str, Any] | None = None,
) -> JobPayment:
    payment_channel = payment_channel or _infer_channel(payment_method)
    payment_status = JobPaymentStatus.MANUAL_PAYMENT_PENDING if payment_method == JobPaymentMethod.CASH else JobPaymentStatus.PENDING
    final_amount = amount if amount is not None else _money(managed_job.client_total)
    payment = JobPayment.objects.create(
        managed_job=managed_job,
        payer=payer,
        amount=final_amount,
        expected_amount=expected_amount if expected_amount is not None else final_amount,
        payment_method=payment_method,
        payment_channel=payment_channel,
        payment_status=payment_status,
        reconciliation_status=JobPaymentReconciliationStatus.PENDING,
        account_reference=account_reference or generate_job_account_reference(managed_job=managed_job),
        payer_phone=payer_phone,
        external_reference=external_reference,
        raw_gateway_payload=raw_gateway_payload,
    )
    initialize_settlement_for_managed_job(managed_job=managed_job, payment_method=payment_method)
    return payment


@transaction.atomic
def mark_payment_confirmed(
    *,
    job_payment: JobPayment,
    raw_gateway_payload: dict[str, Any] | None = None,
) -> JobPayment:
    if job_payment.payment_status != JobPaymentStatus.CONFIRMED:
        job_payment.payment_status = JobPaymentStatus.CONFIRMED
        job_payment.reconciliation_status = JobPaymentReconciliationStatus.CONFIRMED
        job_payment.confirmed_at = timezone.now()
        if raw_gateway_payload is not None:
            job_payment.raw_gateway_payload = raw_gateway_payload
        if job_payment.received_amount in (None, Decimal("0")):
            job_payment.received_amount = job_payment.expected_amount or job_payment.amount
        job_payment.save(update_fields=[
            "payment_status",
            "reconciliation_status",
            "confirmed_at",
            "received_amount",
            "raw_gateway_payload",
            "updated_at",
        ])

    managed_job = job_payment.managed_job
    update_fields = ["payment_status", "payment_confirmed_at", "updated_at"]
    managed_job.payment_status = ManagedJobPaymentStatus.CONFIRMED
    if managed_job.payment_confirmed_at is None:
        managed_job.payment_confirmed_at = job_payment.confirmed_at or timezone.now()
    if managed_job.status == ManagedJobStatus.AWAITING_PAYMENT:
        managed_job.status = ManagedJobStatus.PAYMENT_CONFIRMED
        update_fields.append("status")
    managed_job.save(update_fields=update_fields)

    initialize_settlement_for_managed_job(
        managed_job=managed_job,
        payment_method=job_payment.payment_method,
    )
    record_managed_job_event(
        managed_job=managed_job,
        payment=job_payment,
        actor=None,
        event_type=EVENT_PAYMENT_CONFIRMED,
        summary="Payment confirmed for managed job.",
        metadata={
            "payment_method": job_payment.payment_method,
            "payment_channel": job_payment.payment_channel,
            "amount": str(job_payment.amount),
            "payment_status": job_payment.payment_status,
            "reconciliation_status": job_payment.reconciliation_status,
        },
    )
    return job_payment


@transaction.atomic
def mark_settlement_release_ready(*, settlement: JobSettlementSplit) -> JobSettlementSplit:
    if settlement.status != JobSettlementStatus.RELEASE_READY:
        settlement.status = JobSettlementStatus.RELEASE_READY
        settlement.release_ready_at = timezone.now()
        settlement.save(update_fields=["status", "release_ready_at", "updated_at"])

    managed_job = settlement.managed_job
    managed_job.payment_status = ManagedJobPaymentStatus.RELEASE_READY
    managed_job.save(update_fields=["payment_status", "updated_at"])
    record_managed_job_event(
        managed_job=managed_job,
        settlement=settlement,
        actor=None,
        event_type=EVENT_SETTLEMENT_RELEASE_READY,
        summary="Settlement marked release ready.",
        metadata={"status": settlement.status},
    )
    return settlement


@transaction.atomic
def initiate_job_stk_push(
    *,
    managed_job: ManagedJob,
    payer,
    phone_number: str,
    amount: Decimal | None = None,
) -> JobPayment:
    phone_normalized = normalize_phone_number(phone_number)
    amount_decimal = _money(amount if amount is not None else managed_job.client_total)
    if amount_decimal <= 0:
        raise ValueError("STK push amount must be greater than zero.")

    existing = JobPayment.objects.select_for_update().filter(
        managed_job=managed_job,
        payment_method=JobPaymentMethod.MPESA,
        payment_channel=JobPaymentChannel.STK_PUSH,
        payer=payer,
        expected_amount=amount_decimal,
        payer_phone=phone_normalized,
        payment_status__in=[
            JobPaymentStatus.PENDING,
            JobPaymentStatus.STK_PUSH_SENT,
            JobPaymentStatus.CONFIRMATION_PENDING,
        ],
    ).order_by("-created_at").first()
    if existing and existing.checkout_request_id:
        return existing

    payment = existing or create_job_payment(
        managed_job=managed_job,
        payer=payer,
        amount=amount_decimal,
        expected_amount=amount_decimal,
        payment_method=JobPaymentMethod.MPESA,
        payment_channel=JobPaymentChannel.STK_PUSH,
        payer_phone=phone_normalized,
    )

    config = _get_mpesa_config()
    token = get_mpesa_token()
    password, timestamp = _build_stk_password(config["shortcode"], config["passkey"])
    account_reference = payment.account_reference or generate_job_account_reference(managed_job=managed_job)
    description = _build_job_transaction_description(managed_job)
    payload = {
        "BusinessShortCode": config["shortcode"],
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount_decimal),
        "PartyA": phone_normalized,
        "PartyB": config["shortcode"],
        "PhoneNumber": phone_normalized,
        "CallBackURL": config["callback_url"],
        "AccountReference": account_reference,
        "TransactionDesc": description,
    }

    payment.account_reference = account_reference
    payment.external_reference = account_reference
    payment.raw_gateway_payload = payload
    payment.payment_status = JobPaymentStatus.PENDING
    payment.reconciliation_status = JobPaymentReconciliationStatus.PENDING
    payment.save(update_fields=[
        "account_reference",
        "external_reference",
        "raw_gateway_payload",
        "payment_status",
        "reconciliation_status",
        "updated_at",
    ])

    url = f"{config['base_url']}/mpesa/stkpush/v1/processrequest"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers, timeout=_get_mpesa_timeout())
    response_payload = _safe_json_response(response)
    response.raise_for_status()

    payment.checkout_request_id = _string(response_payload.get("CheckoutRequestID"))
    payment.merchant_request_id = _string(response_payload.get("MerchantRequestID"))
    payment.raw_gateway_payload = {
        "request": payload,
        "response": response_payload,
    }
    payment.payment_status = (
        JobPaymentStatus.STK_PUSH_SENT if _string(response_payload.get("ResponseCode")) == "0"
        else JobPaymentStatus.FAILED
    )
    payment.reconciliation_status = (
        JobPaymentReconciliationStatus.PENDING
        if payment.payment_status == JobPaymentStatus.STK_PUSH_SENT
        else JobPaymentReconciliationStatus.FAILED
    )
    payment.save(update_fields=[
        "checkout_request_id",
        "merchant_request_id",
        "raw_gateway_payload",
        "payment_status",
        "reconciliation_status",
        "updated_at",
    ])

    managed_job.payment_status = ManagedJobPaymentStatus.CONFIRMATION_PENDING
    managed_job.save(update_fields=["payment_status", "updated_at"])
    return payment


@transaction.atomic
def reconcile_job_payment_status(*, job_payment: JobPayment) -> dict[str, Any]:
    if not job_payment.checkout_request_id:
        raise ValueError("Job payment has no checkout_request_id to query.")
    response_payload = query_transaction_status(job_payment.checkout_request_id)
    job_payment.query_payload = response_payload
    result_code = _string(response_payload.get("ResultCode"))
    job_payment.reconciliation_status = JobPaymentReconciliationStatus.CALLBACK_RECEIVED
    if result_code:
        if result_code == "0":
            job_payment.received_amount = job_payment.expected_amount or job_payment.amount
            job_payment.save(update_fields=[
                "query_payload",
                "reconciliation_status",
                "received_amount",
                "updated_at",
            ])
            mark_payment_confirmed(job_payment=job_payment, raw_gateway_payload={"stk_query": response_payload})
            return response_payload
        job_payment.payment_status = _map_job_result_code_to_status(result_code)
        job_payment.reconciliation_status = JobPaymentReconciliationStatus.FAILED
    job_payment.save(update_fields=["query_payload", "payment_status", "reconciliation_status", "updated_at"])
    return response_payload


@transaction.atomic
def handle_job_mpesa_callback(*, payload: dict[str, Any]) -> dict[str, str]:
    if verify_callback_minimally(payload):
        parsed = parse_callback(payload)
        payment = None
        if parsed.get("checkout_request_id"):
            payment = JobPayment.objects.select_for_update().filter(
                checkout_request_id=parsed["checkout_request_id"]
            ).first()
        if payment is None and parsed.get("merchant_request_id"):
            payment = JobPayment.objects.select_for_update().filter(
                merchant_request_id=parsed["merchant_request_id"]
            ).first()
        if payment is None:
            return {"status": "ok", "message": "Payment not found but acknowledged"}

        if payment.payment_status == JobPaymentStatus.CONFIRMED:
            payment.reconciliation_status = JobPaymentReconciliationStatus.DUPLICATE_CALLBACK
            payment.save(update_fields=["reconciliation_status", "updated_at"])
            return {"status": "ok", "message": "Already processed"}

        duplicate = _find_duplicate_receipt(payment=payment, receipt=_string(parsed.get("mpesa_receipt_number")))
        if duplicate is not None:
            payment.reconciliation_status = JobPaymentReconciliationStatus.DUPLICATE_RECEIPT
            payment.payment_status = JobPaymentStatus.FAILED
            payment.callback_payload = payload
            payment.save(update_fields=["reconciliation_status", "payment_status", "callback_payload", "updated_at"])
            return {"status": "ok", "message": "Duplicate receipt acknowledged"}

        payment.callback_payload = payload
        payment.reconciliation_status = JobPaymentReconciliationStatus.CALLBACK_RECEIVED
        payment.checkout_request_id = parsed.get("checkout_request_id", "") or payment.checkout_request_id
        payment.merchant_request_id = parsed.get("merchant_request_id", "") or payment.merchant_request_id
        payment.payer_phone = _string(parsed.get("phone_number"), payment.payer_phone)[:20]
        payment.mpesa_receipt_number = _string(parsed.get("mpesa_receipt_number"))[:50]
        payment.received_amount = parsed.get("amount")
        payment.payment_status = (
            JobPaymentStatus.CONFIRMATION_PENDING if parsed.get("success") else JobPaymentStatus.FAILED
        )
        payment.save(update_fields=[
            "callback_payload",
            "reconciliation_status",
            "checkout_request_id",
            "merchant_request_id",
            "payer_phone",
            "mpesa_receipt_number",
            "received_amount",
            "payment_status",
            "updated_at",
        ])

        expected_amount = payment.expected_amount or payment.amount
        received_amount = payment.received_amount
        if parsed.get("success") and received_amount == expected_amount:
            mark_payment_confirmed(job_payment=payment, raw_gateway_payload={"stk_callback": payload})
            return {"status": "ok", "message": "Processed"}
        if parsed.get("success"):
            payment.reconciliation_status = JobPaymentReconciliationStatus.AMOUNT_MISMATCH
            payment.payment_status = JobPaymentStatus.CONFIRMATION_PENDING
            payment.save(update_fields=["reconciliation_status", "payment_status", "updated_at"])
            return {"status": "ok", "message": "Amount mismatch acknowledged"}
        payment.reconciliation_status = JobPaymentReconciliationStatus.FAILED
        payment.save(update_fields=["reconciliation_status", "updated_at"])
        return {"status": "ok", "message": "Failed callback acknowledged"}

    parsed_c2b = _parse_c2b_callback(payload)
    if not parsed_c2b:
        return {"status": "error", "message": "Invalid callback structure"}

    payment = JobPayment.objects.select_for_update().filter(
        account_reference=parsed_c2b["account_reference"]
    ).order_by("-created_at").first()
    if payment is None:
        return {"status": "ok", "message": "Unknown account reference acknowledged"}

    if payment.payment_status == JobPaymentStatus.CONFIRMED and payment.mpesa_receipt_number == parsed_c2b["mpesa_receipt_number"]:
        payment.reconciliation_status = JobPaymentReconciliationStatus.DUPLICATE_CALLBACK
        payment.save(update_fields=["reconciliation_status", "updated_at"])
        return {"status": "ok", "message": "Already processed"}

    duplicate = _find_duplicate_receipt(payment=payment, receipt=parsed_c2b["mpesa_receipt_number"])
    if duplicate is not None:
        payment.reconciliation_status = JobPaymentReconciliationStatus.DUPLICATE_RECEIPT
        payment.payment_status = JobPaymentStatus.FAILED
        payment.callback_payload = parsed_c2b["raw_payload"]
        payment.save(update_fields=["reconciliation_status", "payment_status", "callback_payload", "updated_at"])
        return {"status": "ok", "message": "Duplicate receipt acknowledged"}

    payment.payment_channel = JobPaymentChannel.PAYBILL_MANUAL
    payment.payment_method = JobPaymentMethod.MPESA
    payment.payment_status = JobPaymentStatus.CONFIRMATION_PENDING
    payment.reconciliation_status = JobPaymentReconciliationStatus.CALLBACK_RECEIVED
    payment.callback_payload = parsed_c2b["raw_payload"]
    payment.mpesa_receipt_number = parsed_c2b["mpesa_receipt_number"][:50]
    payment.payer_phone = parsed_c2b["payer_phone"][:20]
    payment.received_amount = parsed_c2b["received_amount"]
    payment.save(update_fields=[
        "payment_channel",
        "payment_method",
        "payment_status",
        "reconciliation_status",
        "callback_payload",
        "mpesa_receipt_number",
        "payer_phone",
        "received_amount",
        "updated_at",
    ])

    expected_amount = payment.expected_amount or payment.amount
    if payment.received_amount == expected_amount:
        mark_payment_confirmed(job_payment=payment, raw_gateway_payload={"paybill_callback": parsed_c2b["raw_payload"]})
        return {"status": "ok", "message": "Processed"}

    payment.reconciliation_status = JobPaymentReconciliationStatus.AMOUNT_MISMATCH
    payment.save(update_fields=["reconciliation_status", "updated_at"])
    return {"status": "ok", "message": "Amount mismatch acknowledged"}
