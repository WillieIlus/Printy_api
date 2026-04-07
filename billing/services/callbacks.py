"""Callback service — idempotent processing of Daraja STK push callbacks."""
from __future__ import annotations

import logging

from django.db import transaction

from billing.models import PaymentTransaction, RenewalAttempt
from billing.services.payments import parse_callback, record_callback, verify_callback_minimally

logger = logging.getLogger("payments")


def handle_mpesa_callback(payload: dict) -> dict:
    """
    Idempotent entry point for all Daraja STK push callbacks.

    Returns: {"status": "ok"|"error", "message": str}
    """
    if not verify_callback_minimally(payload):
        logger.warning("Received malformed M-Pesa callback: %s", str(payload)[:200])
        return {"status": "error", "message": "Invalid callback structure"}

    parsed = parse_callback(payload)
    checkout_id = parsed.get("checkout_request_id", "")
    merchant_id = parsed.get("merchant_request_id", "")

    # Locate the transaction — prefer checkout_request_id
    txn = None
    if checkout_id:
        txn = PaymentTransaction.objects.filter(checkout_request_id=checkout_id).first()
    if txn is None and merchant_id:
        txn = PaymentTransaction.objects.filter(merchant_request_id=merchant_id).first()

    if txn is None:
        logger.warning(
            "Callback for unknown transaction: checkout=%s merchant=%s", checkout_id, merchant_id
        )
        # Return 200 to Daraja to prevent retries for unknown transactions
        return {"status": "ok", "message": "Transaction not found — acknowledged"}

    # Idempotency: skip if already processed
    if txn.status in (PaymentTransaction.STATUS_SUCCESS, PaymentTransaction.STATUS_FAILED):
        logger.info(
            "Duplicate callback for txn %s (already %s) — acknowledged without side effects.",
            txn.id,
            txn.status,
        )
        return {"status": "ok", "message": "Already processed"}

    # Duplicate receipt guard
    receipt = parsed.get("mpesa_receipt_number")
    if receipt and PaymentTransaction.objects.filter(mpesa_receipt_number=receipt).exclude(id=txn.id).exists():
        logger.warning("Duplicate M-Pesa receipt %s for txn %s — rejecting.", receipt, txn.id)
        txn.status = PaymentTransaction.STATUS_FAILED
        txn.result_desc = "Duplicate receipt number"
        txn.save(update_fields=["status", "result_desc", "updated_at"])
        return {"status": "ok", "message": "Duplicate receipt — acknowledged"}

    with transaction.atomic():
        record_callback(txn, parsed, payload)
        txn.refresh_from_db()

        if txn.status == PaymentTransaction.STATUS_SUCCESS:
            _handle_success(txn)
        else:
            _handle_failure(txn, parsed.get("result_desc", "Payment failed"))

    return {"status": "ok", "message": "Processed"}


def _handle_success(txn: PaymentTransaction) -> None:
    """Activate / extend subscription after a confirmed successful payment."""
    from billing.services.subscriptions import activate_subscription_from_successful_payment

    if txn.subscription_id is None:
        logger.warning("Successful txn %s has no subscription — skipping activation.", txn.id)
        return

    sub = activate_subscription_from_successful_payment(txn)
    logger.info(
        "Subscription %s activated/extended via txn %s (receipt=%s).",
        sub.id, txn.id, txn.mpesa_receipt_number,
    )

    # Mark any linked RenewalAttempt as success
    RenewalAttempt.objects.filter(
        payment_transaction=txn,
        status__in=[RenewalAttempt.STATUS_INITIATED, RenewalAttempt.STATUS_AWAITING],
    ).update(status=RenewalAttempt.STATUS_SUCCESS)


def _handle_failure(txn: PaymentTransaction, reason: str) -> None:
    """React to a failed payment — may trigger retry or grace period transition."""
    from billing.services.renewals import handle_renewal_failure

    logger.info("Payment failed for txn %s: %s", txn.id, reason)

    # Propagate failure to any linked renewal attempt
    attempt = RenewalAttempt.objects.filter(
        payment_transaction=txn,
        status__in=[RenewalAttempt.STATUS_INITIATED, RenewalAttempt.STATUS_AWAITING],
    ).first()

    if attempt:
        handle_renewal_failure(attempt, reason)
