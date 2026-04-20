"""Renewal service — scheduled STK push retry logic and grace period escalation."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing.models import RenewalAttempt, ShopSubscription, PaymentTransaction

logger = logging.getLogger("payments")

RETRY_SCHEDULE_HOURS: list[int] = getattr(settings, "BILLING_RETRY_SCHEDULE_HOURS", [6, 24, 48])
MAX_ATTEMPTS = len(RETRY_SCHEDULE_HOURS) + 1  # initial + retries


# ---------------------------------------------------------------------------
# Provider adapter skeleton (future-proof)
# ---------------------------------------------------------------------------

class BaseRecurringBillingProvider:
    def initiate_renewal(self, subscription: ShopSubscription, attempt: RenewalAttempt) -> PaymentTransaction:
        raise NotImplementedError


class DarajaManualRenewalProvider(BaseRecurringBillingProvider):
    """Current implementation: fire an STK Push for each renewal attempt."""

    def initiate_renewal(self, subscription: ShopSubscription, attempt: RenewalAttempt) -> PaymentTransaction:
        from billing.services.subscriptions import renew_subscription
        txn = renew_subscription(subscription)
        return txn


class MpesaRatibaProvider(BaseRecurringBillingProvider):
    """Placeholder — Ratiba direct debit API not yet public. Do not implement."""

    def initiate_renewal(self, subscription: ShopSubscription, attempt: RenewalAttempt) -> PaymentTransaction:
        raise NotImplementedError("Ratiba direct debit is not yet available.")


_provider = DarajaManualRenewalProvider()


# ---------------------------------------------------------------------------
# Queue / process helpers
# ---------------------------------------------------------------------------

def queue_due_renewals() -> int:
    """Create RenewalAttempt(queued) for subscriptions whose renews_at is now past."""
    now = timezone.now()
    from billing.models import Plan as BillingPlan
    due_subs = ShopSubscription.objects.filter(
        status__in=[ShopSubscription.STATUS_ACTIVE, ShopSubscription.STATUS_TRIALING],
        auto_renew_enabled=True,
        renews_at__lte=now,
    ).exclude(plan__code=BillingPlan.CODE_FREE)  # never queue the Free plan

    created = 0
    for sub in due_subs:
        # Avoid duplicate queued attempts for the same due_at window
        already_queued = RenewalAttempt.objects.filter(
            subscription=sub,
            due_at=sub.renews_at,
            status__in=[
                RenewalAttempt.STATUS_QUEUED,
                RenewalAttempt.STATUS_INITIATED,
                RenewalAttempt.STATUS_AWAITING,
            ],
        ).exists()
        if not already_queued:
            RenewalAttempt.objects.create(
                subscription=sub,
                due_at=sub.renews_at,
                attempt_number=1,
                status=RenewalAttempt.STATUS_QUEUED,
            )
            created += 1
    return created


def process_due_renewals() -> int:
    """Fire STK push for all queued renewal attempts that are due."""
    now = timezone.now()
    attempts = RenewalAttempt.objects.filter(
        status=RenewalAttempt.STATUS_QUEUED,
        due_at__lte=now,
    ).select_related("subscription__plan", "subscription__owner")

    processed = 0
    for attempt in attempts:
        sub = attempt.subscription
        if not sub.payment_phone_e164:
            attempt.status = RenewalAttempt.STATUS_FAILED
            attempt.failure_reason = "No payment phone on file"
            attempt.save(update_fields=["status", "failure_reason", "updated_at"])
            _escalate_to_past_due_if_exhausted(sub, attempt)
            continue

        attempt.status = RenewalAttempt.STATUS_INITIATED
        attempt.attempted_at = now
        attempt.save(update_fields=["status", "attempted_at", "updated_at"])

        try:
            txn = _provider.initiate_renewal(sub, attempt)
            attempt.payment_transaction = txn
            attempt.status = RenewalAttempt.STATUS_AWAITING
            attempt.save(update_fields=["payment_transaction", "status", "updated_at"])
            processed += 1
        except Exception as exc:
            logger.exception("Renewal initiation failed for sub %s: %s", sub.id, exc)
            attempt.status = RenewalAttempt.STATUS_FAILED
            attempt.failure_reason = str(exc)[:255]
            attempt.save(update_fields=["status", "failure_reason", "updated_at"])
            _handle_retry_or_escalate(sub, attempt)

    return processed


def process_timed_out_renewals() -> int:
    """Mark awaiting attempts as timed_out if no callback received in time."""
    from django.conf import settings as s
    timeout_seconds = getattr(s, "MPESA_TIMEOUT_SECONDS", 120)
    cutoff = timezone.now() - timedelta(seconds=timeout_seconds + 60)

    awaiting = RenewalAttempt.objects.filter(
        status=RenewalAttempt.STATUS_AWAITING,
        attempted_at__lt=cutoff,
    ).select_related("subscription", "payment_transaction")

    count = 0
    for attempt in awaiting:
        txn = attempt.payment_transaction
        if txn and txn.status in (PaymentTransaction.STATUS_PENDING, PaymentTransaction.STATUS_PROCESSING):
            txn.status = PaymentTransaction.STATUS_TIMED_OUT
            txn.completed_at = timezone.now()
            txn.save(update_fields=["status", "completed_at", "updated_at"])

        attempt.status = RenewalAttempt.STATUS_FAILED
        attempt.failure_reason = "No callback received (timed out)"
        attempt.save(update_fields=["status", "failure_reason", "updated_at"])

        _handle_retry_or_escalate(attempt.subscription, attempt)
        count += 1
    return count


def handle_renewal_failure(attempt: RenewalAttempt, reason: str) -> None:
    """Called by callbacks.py when a payment fails."""
    attempt.status = RenewalAttempt.STATUS_FAILED
    attempt.failure_reason = reason[:255]
    attempt.save(update_fields=["status", "failure_reason", "updated_at"])
    _handle_retry_or_escalate(attempt.subscription, attempt)


def _handle_retry_or_escalate(subscription: ShopSubscription, last_attempt: RenewalAttempt) -> None:
    """Schedule next retry or escalate to past_due / grace."""
    attempt_number = last_attempt.attempt_number

    if attempt_number <= len(RETRY_SCHEDULE_HOURS):
        hours = RETRY_SCHEDULE_HOURS[attempt_number - 1]
        next_time = timezone.now() + timedelta(hours=hours)
        RenewalAttempt.objects.create(
            subscription=subscription,
            due_at=next_time,
            attempt_number=attempt_number + 1,
            status=RenewalAttempt.STATUS_QUEUED,
        )
        subscription.next_renewal_attempt_at = next_time
        subscription.save(update_fields=["next_renewal_attempt_at", "updated_at"])
    else:
        _escalate_to_past_due_if_exhausted(subscription, last_attempt)


def _escalate_to_past_due_if_exhausted(subscription: ShopSubscription, attempt: RenewalAttempt) -> None:
    """All retries exhausted — move directly to grace_period (skipping a redundant past_due save)."""
    from billing.services.subscriptions import move_to_grace_period
    # Re-check status inside the atomic block after a fresh read to close the race window
    # between the caller's status check and acquiring the row lock.
    with transaction.atomic():
        subscription.refresh_from_db()
        if subscription.status in (
            ShopSubscription.STATUS_PAST_DUE,
            ShopSubscription.STATUS_GRACE,
            ShopSubscription.STATUS_SUSPENDED,
        ):
            return
        # Mark past_due in memory only — move_to_grace_period will save the final state,
        # so we avoid a redundant intermediate DB write.
        subscription.status = ShopSubscription.STATUS_PAST_DUE
        move_to_grace_period(subscription, reason="Renewal retries exhausted")
    logger.info("Subscription %s moved to grace_period after failed renewals.", subscription.id)


def expire_grace_periods() -> int:
    """Suspend any subscriptions whose grace period has ended."""
    from billing.services.subscriptions import suspend_if_grace_expired
    grace_subs = ShopSubscription.objects.filter(
        status=ShopSubscription.STATUS_GRACE,
        grace_period_ends_at__lte=timezone.now(),
    )
    count = 0
    for sub in grace_subs:
        if suspend_if_grace_expired(sub):
            count += 1
    return count
