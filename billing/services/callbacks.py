"""Unified callback service for idempotent Daraja M-Pesa processing."""
from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from billing.models import PaymentTransaction, RenewalAttempt
from billing.services.payments import (
    parse_callback,
    verify_callback_minimally,
)
from common.payment_constants import PaymentStatus

logger = logging.getLogger("payments")


def handle_mpesa_callback(payload: dict) -> dict[str, str]:
    """
    Unified entry point for all M-Pesa callbacks (STK Push and C2B).
    Dispatches to appropriate model handlers based on identifiers.
    """
    # 1. Try STK Push parsing
    if verify_callback_minimally(payload):
        return _handle_stk_callback(payload)

    # 2. Try C2B (Paybill) parsing
    from jobs.payment_services import _parse_c2b_callback
    parsed_c2b = _parse_c2b_callback(payload)
    if parsed_c2b:
        return _handle_c2b_callback(parsed_c2b, payload)

    logger.warning("Callback did not match any canonical payment structure: %s", payload)
    return {"handled": False, "status": "error", "message": "Payment callback structure not recognized"}


def _handle_stk_callback(payload: dict) -> dict[str, str]:
    parsed = parse_callback(payload)
    checkout_id = parsed.get("checkout_request_id", "")
    merchant_id = parsed.get("merchant_request_id", "")

    # Identify which model this belongs to
    # Billing
    txn = _find_billing_transaction(checkout_id, merchant_id)
    if txn:
        return _process_billing_stk(txn, parsed, payload)

    # Jobs
    job_payment = _find_job_payment(checkout_id, merchant_id)
    if job_payment:
        return _process_job_stk(job_payment, parsed, payload)

    # Subscriptions (Legacy)
    stk_req = _find_subscription_stk(checkout_id)
    if stk_req:
        return _process_subscription_stk(stk_req, parsed, payload)

    logger.warning(
        "STK callback for unknown transaction checkout=%s merchant=%s",
        checkout_id,
        merchant_id,
    )
    return {"handled": False, "status": "ok", "message": "Transaction not found"}


def _handle_c2b_callback(parsed_c2b: dict, raw_payload: dict) -> dict[str, str]:
    account_ref = parsed_c2b.get("account_reference", "")
    
    # Currently only Jobs handles C2B via account reference
    from jobs.models import JobPayment
    payment = JobPayment.objects.filter(account_reference=account_ref).order_by("-created_at").first()
    
    if payment:
        from jobs.payment_services import mark_payment_confirmed, _find_duplicate_receipt
        with transaction.atomic():
            payment = JobPayment.objects.select_for_update().get(id=payment.id)
            
            # Idempotency
            if payment.payment_status == PaymentStatus.PAID and payment.mpesa_receipt_number == parsed_c2b["mpesa_receipt_number"]:
                return {"handled": True, "status": "ok", "message": "Already processed"}

            # Duplicate receipt check
            duplicate = _find_duplicate_receipt(payment=payment, receipt=parsed_c2b["mpesa_receipt_number"])
            if duplicate:
                payment.payment_status = PaymentStatus.NEEDS_REVIEW
                payment.reconciliation_status = "duplicate_receipt"
                payment.save(update_fields=["payment_status", "reconciliation_status", "updated_at"])
                return {"handled": True, "status": "ok", "message": "Duplicate receipt"}

            payment.callback_payload = raw_payload
            payment.mpesa_receipt_number = parsed_c2b["mpesa_receipt_number"]
            payment.received_amount = parsed_c2b["received_amount"]
            payment.payer_phone = parsed_c2b.get("payer_phone", "")
            payment.reconciliation_status = "callback_received"
            
            expected = payment.expected_amount or payment.amount
            if payment.received_amount == expected:
                payment.save(update_fields=["callback_payload", "mpesa_receipt_number", "received_amount", "payer_phone", "reconciliation_status", "updated_at"])
                mark_payment_confirmed(job_payment=payment, raw_gateway_payload={"c2b_callback": raw_payload})
                return {"handled": True, "status": "ok", "message": "Processed"}
            else:
                payment.payment_status = PaymentStatus.NEEDS_REVIEW
                payment.reconciliation_status = "amount_mismatch"
                payment.save(update_fields=["callback_payload", "mpesa_receipt_number", "received_amount", "payer_phone", "payment_status", "reconciliation_status", "updated_at"])
                return {"handled": True, "status": "ok", "message": "Amount mismatch; needs review"}

    return {"handled": False, "status": "ok", "message": "Unknown account reference"}


# --- Helper Finders ---

def _find_billing_transaction(checkout_id: str, merchant_id: str) -> PaymentTransaction | None:
    txn = None
    if checkout_id:
        txn = PaymentTransaction.objects.filter(checkout_request_id=checkout_id).first()
    if txn is None and merchant_id:
        txn = PaymentTransaction.objects.filter(merchant_request_id=merchant_id).first()
    return txn


def _find_job_payment(checkout_id: str, merchant_id: str) -> Any | None:
    from jobs.models import JobPayment
    payment = None
    if checkout_id:
        payment = JobPayment.objects.filter(checkout_request_id=checkout_id).first()
    if payment is None and merchant_id:
        payment = JobPayment.objects.filter(merchant_request_id=merchant_id).first()
    return payment


def _find_subscription_stk(checkout_id: str) -> Any | None:
    from subscriptions.models import MpesaStkRequest
    if not checkout_id:
        return None
    return MpesaStkRequest.objects.filter(checkout_request_id=checkout_id).first()


# --- Per-Model Processing Logic ---

def _process_billing_stk(txn: PaymentTransaction, parsed: dict, payload: dict) -> dict[str, str]:
    with transaction.atomic():
        txn = PaymentTransaction.objects.select_for_update().get(id=txn.id)
        
        if txn.status in {PaymentStatus.PAID, PaymentStatus.FAILED, PaymentStatus.CANCELLED}:
            return {"handled": True, "status": "ok", "message": "Already processed"}

        receipt = parsed.get("mpesa_receipt_number")
        if receipt and PaymentTransaction.objects.filter(mpesa_receipt_number=receipt).exclude(id=txn.id).exists():
            txn.status = PaymentStatus.FAILED
            txn.result_desc = "Duplicate receipt number"
            txn.save(update_fields=["status", "result_desc", "updated_at"])
            return {"handled": True, "status": "ok", "message": "Duplicate receipt"}

        # Basic record
        txn.raw_callback = payload
        txn.callback_received_at = timezone.now()
        txn.result_code = parsed["result_code"]
        txn.result_desc = parsed["result_desc"][:255]
        txn.mpesa_receipt_number = (receipt or "")[:50]
        
        if not parsed.get("success"):
            txn.status = PaymentStatus.CANCELLED if parsed.get("result_code") == "1032" else PaymentStatus.FAILED
            txn.completed_at = timezone.now()
            txn.save()
            _handle_billing_failure(txn, txn.result_desc)
            return {"handled": True, "status": "ok", "message": "Failed callback acknowledged"}

        if not receipt:
            txn.status = PaymentStatus.NEEDS_REVIEW
            txn.save()
            return {"handled": True, "status": "ok", "message": "Receipt missing; needs review"}

        # Amount verification
        received = parsed.get("amount")
        expected = txn.amount
        if txn.transaction_type == PaymentTransaction.TYPE_SANDBOX_TEST:
            expected = received
        if received != expected:
            logger.warning("Amount mismatch for billing txn %s: expected %s, got %s", txn.id, expected, received)
            txn.status = PaymentStatus.NEEDS_REVIEW
            txn.save()
            return {"handled": True, "status": "ok", "message": "Amount mismatch; needs review"}

        txn.status = PaymentStatus.PAID
        txn.completed_at = timezone.now()
        txn.save()
        _handle_billing_success(txn)

    return {"handled": True, "status": "ok", "message": "Processed"}


def _process_job_stk(payment: Any, parsed: dict, payload: dict) -> dict[str, str]:
    from jobs.payment_services import mark_payment_confirmed, _find_duplicate_receipt
    from jobs.models import JobPayment
    
    with transaction.atomic():
        payment = JobPayment.objects.select_for_update().get(id=payment.id)
        
        if payment.payment_status == PaymentStatus.PAID:
            return {"handled": True, "status": "ok", "message": "Already processed"}

        receipt = parsed.get("mpesa_receipt_number")
        duplicate = _find_duplicate_receipt(payment=payment, receipt=receipt or "")
        if duplicate:
            payment.payment_status = PaymentStatus.NEEDS_REVIEW
            payment.reconciliation_status = "duplicate_receipt"
            payment.save(update_fields=["payment_status", "reconciliation_status", "updated_at"])
            return {"handled": True, "status": "ok", "message": "Duplicate receipt"}

        payment.callback_payload = payload
        payment.mpesa_receipt_number = (receipt or "")[:50]
        payment.received_amount = parsed.get("amount")
        payment.reconciliation_status = "callback_received"
        
        if not parsed.get("success"):
            payment.payment_status = PaymentStatus.CANCELLED if parsed.get("result_code") == "1032" else PaymentStatus.FAILED
            payment.reconciliation_status = "failed"
            payment.save(update_fields=["callback_payload", "mpesa_receipt_number", "received_amount", "payment_status", "reconciliation_status", "updated_at"])
            return {"handled": True, "status": "ok", "message": "Failed callback acknowledged"}

        if not receipt:
            payment.payment_status = PaymentStatus.NEEDS_REVIEW
            payment.reconciliation_status = "manual_review"
            payment.save(update_fields=["callback_payload", "mpesa_receipt_number", "received_amount", "payment_status", "reconciliation_status", "updated_at"])
            return {"handled": True, "status": "ok", "message": "Receipt missing; needs review"}

        # Amount verification
        received = payment.received_amount
        expected = payment.expected_amount or payment.amount
        if received != expected:
            payment.payment_status = PaymentStatus.NEEDS_REVIEW
            payment.reconciliation_status = "amount_mismatch"
            payment.save(update_fields=["callback_payload", "mpesa_receipt_number", "received_amount", "payment_status", "reconciliation_status", "updated_at"])
            return {"handled": True, "status": "ok", "message": "Amount mismatch; needs review"}

        payment.save(update_fields=["callback_payload", "mpesa_receipt_number", "received_amount", "reconciliation_status", "updated_at"])
        mark_payment_confirmed(job_payment=payment, raw_gateway_payload={"stk_callback": payload})

    return {"handled": True, "status": "ok", "message": "Processed"}


def _process_subscription_stk(stk_req: Any, parsed: dict, payload: dict) -> dict[str, str]:
    from subscriptions.models import MpesaStkRequest
    from subscriptions.services.callbacks import _complete_legacy_subscription
    
    with transaction.atomic():
        stk_req = MpesaStkRequest.objects.select_for_update().get(id=stk_req.id)
        
        if stk_req.status == PaymentStatus.PAID:
            return {"handled": True, "status": "ok", "message": "Already processed"}

        stk_req.raw_callback_payload = payload
        
        if not parsed.get("success"):
            stk_req.status = PaymentStatus.FAILED
            stk_req.save()
            return {"handled": True, "status": "ok", "message": "Failed callback acknowledged"}

        receipt = parsed.get("mpesa_receipt_number")
        if not receipt:
            stk_req.status = PaymentStatus.NEEDS_REVIEW
            stk_req.save()
            return {"handled": True, "status": "ok", "message": "Receipt missing; needs review"}

        # Amount verification
        received = parsed.get("amount")
        expected = stk_req.amount
        if received != expected:
            stk_req.status = PaymentStatus.NEEDS_REVIEW
            stk_req.save()
            return {"handled": True, "status": "ok", "message": "Amount mismatch; needs review"}

        stk_req.receipt_number = receipt
        stk_req.status = PaymentStatus.PAID
        stk_req.save()
        
        _complete_legacy_subscription(stk_req)

    return {"handled": True, "status": "ok", "message": "Processed"}


# --- Post-Processing Hooks ---

def _handle_billing_success(txn: PaymentTransaction) -> None:
    if txn.transaction_type == PaymentTransaction.TYPE_SANDBOX_TEST:
        return

    from billing.services.subscriptions import activate_subscription_from_successful_payment
    if txn.subscription_id:
        activate_subscription_from_successful_payment(txn)
        RenewalAttempt.objects.filter(
            payment_transaction=txn,
            status__in=[RenewalAttempt.STATUS_INITIATED, RenewalAttempt.STATUS_AWAITING],
        ).update(status=RenewalAttempt.STATUS_SUCCESS)


def _handle_billing_failure(txn: PaymentTransaction, reason: str) -> None:
    if txn.transaction_type == PaymentTransaction.TYPE_SANDBOX_TEST:
        return

    from billing.services.renewals import handle_renewal_failure
    attempt = RenewalAttempt.objects.filter(
        payment_transaction=txn,
        status__in=[RenewalAttempt.STATUS_INITIATED, RenewalAttempt.STATUS_AWAITING],
    ).first()
    if attempt:
        handle_renewal_failure(attempt, reason)
