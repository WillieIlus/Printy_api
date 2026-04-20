"""Callback service for idempotent Daraja STK push processing."""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from billing.models import PaymentTransaction, RenewalAttempt
from billing.services.payments import parse_callback, record_callback, verify_callback_minimally

logger = logging.getLogger("payments")


def handle_mpesa_callback(payload: dict) -> dict[str, str]:
    if not verify_callback_minimally(payload):
        logger.warning("Received malformed billing M-Pesa callback.")
        return {"status": "error", "message": "Invalid callback structure"}

    parsed = parse_callback(payload)
    checkout_id = parsed.get("checkout_request_id", "")
    merchant_id = parsed.get("merchant_request_id", "")

    with transaction.atomic():
        txn = _find_transaction_for_update(checkout_id, merchant_id)
        if txn is None:
            logger.warning(
                "Callback for unknown billing transaction checkout=%s merchant=%s",
                checkout_id,
                merchant_id,
            )
            return {"status": "ok", "message": "Transaction not found but acknowledged"}

        if txn.status in {
            PaymentTransaction.STATUS_SUCCESS,
            PaymentTransaction.STATUS_FAILED,
            PaymentTransaction.STATUS_CANCELLED,
            PaymentTransaction.STATUS_TIMED_OUT,  # late callback after timeout must not reprocess
        }:
            logger.info("Duplicate callback for billing transaction %s ignored.", txn.id)
            return {"status": "ok", "message": "Already processed"}

        receipt = parsed.get("mpesa_receipt_number")
        if receipt and PaymentTransaction.objects.select_for_update().filter(
            mpesa_receipt_number=receipt
        ).exclude(id=txn.id).exists():
            logger.warning("Duplicate M-Pesa receipt %s detected for billing transaction %s.", receipt, txn.id)
            txn.status = PaymentTransaction.STATUS_FAILED
            txn.result_desc = "Duplicate receipt number"
            txn.save(update_fields=["status", "result_desc", "updated_at"])
            return {"status": "ok", "message": "Duplicate receipt acknowledged"}

        if parsed.get("success") and not receipt:
            txn.raw_callback = payload
            txn.callback_received_at = timezone.now()
            txn.result_code = parsed["result_code"]
            txn.result_desc = "Success callback missing MpesaReceiptNumber"
            txn.save(update_fields=["raw_callback", "result_code", "result_desc", "updated_at"])
            logger.warning("Successful callback for billing transaction %s missing receipt number.", txn.id)
            return {"status": "ok", "message": "Receipt missing; acknowledged for manual review"}

        record_callback(txn, parsed, payload)
        txn.refresh_from_db()

        if txn.status == PaymentTransaction.STATUS_SUCCESS:
            _handle_success(txn)
        else:
            _handle_failure(txn, parsed.get("result_desc", "Payment failed"))

    return {"status": "ok", "message": "Processed"}


def _find_transaction_for_update(checkout_id: str, merchant_id: str) -> PaymentTransaction | None:
    txn = None
    if checkout_id:
        txn = PaymentTransaction.objects.select_for_update().filter(checkout_request_id=checkout_id).first()
    if txn is None and merchant_id:
        txn = PaymentTransaction.objects.select_for_update().filter(merchant_request_id=merchant_id).first()
    return txn


def _handle_success(txn: PaymentTransaction) -> None:
    if txn.transaction_type == PaymentTransaction.TYPE_SANDBOX_TEST:
        logger.info(
            "Sandbox test transaction %s marked successful with receipt=%s; no subscription activation performed.",
            txn.id,
            txn.mpesa_receipt_number,
        )
        return

    from billing.services.subscriptions import activate_subscription_from_successful_payment

    if txn.subscription_id is None:
        logger.warning("Successful billing transaction %s has no subscription linked.", txn.id)
        return

    sub = activate_subscription_from_successful_payment(txn)
    logger.info(
        "Subscription %s activated or extended by billing transaction %s (receipt=%s).",
        sub.id,
        txn.id,
        txn.mpesa_receipt_number,
    )
    RenewalAttempt.objects.filter(
        payment_transaction=txn,
        status__in=[RenewalAttempt.STATUS_INITIATED, RenewalAttempt.STATUS_AWAITING],
    ).update(status=RenewalAttempt.STATUS_SUCCESS)


def _handle_failure(txn: PaymentTransaction, reason: str) -> None:
    if txn.transaction_type == PaymentTransaction.TYPE_SANDBOX_TEST:
        logger.info("Sandbox test transaction %s failed: %s", txn.id, reason)
        return

    from billing.services.renewals import handle_renewal_failure

    logger.info("Billing payment transaction %s failed: %s", txn.id, reason)
    attempt = RenewalAttempt.objects.filter(
        payment_transaction=txn,
        status__in=[RenewalAttempt.STATUS_INITIATED, RenewalAttempt.STATUS_AWAITING],
    ).first()
    if attempt:
        handle_renewal_failure(attempt, reason)
