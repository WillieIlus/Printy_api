"""Subscription service — lifecycle management for ShopSubscription."""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing.models import (
    Plan,
    ShopSubscription,
    SubscriptionShop,
    PaymentTransaction,
    UsageCounter,
)
from billing.services.payments import initiate_stk_push, normalize_phone_number

if TYPE_CHECKING:
    from accounts.models import User
    from shops.models import Shop

logger = logging.getLogger("payments")

GRACE_PERIOD_DAYS = getattr(settings, "BILLING_GRACE_PERIOD_DAYS", 3)
RETRY_SCHEDULE_HOURS = getattr(settings, "BILLING_RETRY_SCHEDULE_HOURS", [6, 24, 48])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extend_subscription(subscription: ShopSubscription) -> None:
    """Extend ends_at / renews_at from max(now, ends_at) by one billing period."""
    now = timezone.now()
    base = max(now, subscription.ends_at) if subscription.ends_at else now

    if subscription.billing_interval == ShopSubscription.INTERVAL_ANNUAL:
        new_end = base + timedelta(days=365)
    else:
        new_end = base + timedelta(days=30)

    subscription.ends_at = new_end
    subscription.renews_at = new_end


def _retire_other_current_subscriptions(subscription: ShopSubscription) -> None:
    """Ensure a newly paid subscription becomes the single current subscription for the owner."""
    now = timezone.now()
    other_subs = (
        ShopSubscription.objects.filter(owner=subscription.owner)
        .exclude(id=subscription.id)
        .exclude(status__in=[ShopSubscription.STATUS_CANCELLED, ShopSubscription.STATUS_EXPIRED])
    )
    for other in other_subs:
        note = f"Superseded by subscription #{subscription.id} on {now.isoformat()}"
        other.status = ShopSubscription.STATUS_EXPIRED
        other.auto_renew_enabled = False
        other.ends_at = other.ends_at or now
        other.renews_at = None
        other.grace_period_ends_at = None
        other.cancelled_at = other.cancelled_at or now
        other.notes = "\n".join(filter(None, [other.notes.strip(), note])).strip()
        other.save(
            update_fields=[
                "status",
                "auto_renew_enabled",
                "ends_at",
                "renews_at",
                "grace_period_ends_at",
                "cancelled_at",
                "notes",
                "updated_at",
            ]
        )


def _attach_shops(subscription: ShopSubscription, shop_ids: list[int]) -> None:
    from shops.models import Shop as ShopModel
    shops = ShopModel.objects.filter(id__in=shop_ids, owner=subscription.owner)
    for idx, shop in enumerate(shops):
        SubscriptionShop.objects.get_or_create(
            subscription=subscription,
            shop=shop,
            defaults={"is_primary": idx == 0},
        )


def _compute_over_limit(subscription: ShopSubscription) -> bool:
    """Check whether owner's actual usage exceeds the plan limits."""
    from billing.services.entitlements import get_current_usage, get_plan_limits
    usage = get_current_usage(subscription.owner)
    limits = get_plan_limits(subscription)

    def exceeds(used: int, limit: int | None) -> bool:
        return limit is not None and used > limit

    return (
        exceeds(usage["shops"], limits["shops_limit"])
        or exceeds(usage["machines"], limits["machines_limit"])
        or exceeds(usage["active_products"], limits["products_limit"])
        or exceeds(usage["team_members"], limits["users_limit"])
    )


# ---------------------------------------------------------------------------
# Public subscription service API
# ---------------------------------------------------------------------------

def get_or_create_free_subscription(owner) -> ShopSubscription:
    """Every owner has at least a Free subscription.  Create one if missing."""
    from billing.services.plans import get_free_plan
    existing = (
        ShopSubscription.objects.filter(
            owner=owner,
            status__in=[
                ShopSubscription.STATUS_ACTIVE,
                ShopSubscription.STATUS_TRIALING,
                ShopSubscription.STATUS_PAST_DUE,
                ShopSubscription.STATUS_GRACE,
            ],
        )
        .select_related("plan")
        .first()
    )
    if existing:
        return existing

    free_plan = get_free_plan()
    with transaction.atomic():
        sub = ShopSubscription.objects.create(
            owner=owner,
            plan=free_plan,
            billing_interval=ShopSubscription.INTERVAL_MONTHLY,
            status=ShopSubscription.STATUS_ACTIVE,
            starts_at=timezone.now(),
            ends_at=None,  # Free never expires
        )
        # Attach owner's first shop if present
        from shops.models import Shop as ShopModel
        first_shop = ShopModel.objects.filter(owner=owner).order_by("id").first()
        if first_shop:
            SubscriptionShop.objects.create(subscription=sub, shop=first_shop, is_primary=True)
    return sub


def validate_shop_selection_against_plan(plan: Plan, shop_ids: list[int]) -> None:
    """Raise ValueError when shop_ids count exceeds the plan's shop limit."""
    if len(shop_ids) > plan.shops_limit:
        raise ValueError(
            f"Plan '{plan.name}' allows {plan.shops_limit} shop(s); "
            f"you selected {len(shop_ids)}."
        )


def attach_allowed_shops(subscription: ShopSubscription, shop_ids: list[int]) -> None:
    validate_shop_selection_against_plan(subscription.plan, shop_ids)
    _attach_shops(subscription, shop_ids)


def subscribe_to_plan(
    *,
    owner,
    plan_code: str,
    billing_interval: str,
    phone_number: str,
    selected_shop_ids: list[int],
) -> PaymentTransaction:
    """
    Initiate payment for a new or first-time plan subscription.
    Subscription is activated by callbacks.py after successful payment.
    """
    from billing.services.plans import get_plan_by_code
    plan = get_plan_by_code(plan_code)

    if plan.is_free:
        raise ValueError("Use get_or_create_free_subscription for the Free plan.")

    validate_shop_selection_against_plan(plan, selected_shop_ids)

    phone_normalized = normalize_phone_number(phone_number)
    amount = plan.get_price(billing_interval)

    # Create a pending subscription to attach to the transaction
    with transaction.atomic():
        sub = ShopSubscription.objects.create(
            owner=owner,
            plan=plan,
            billing_interval=billing_interval,
            status=ShopSubscription.STATUS_TRIALING,
            starts_at=timezone.now(),
            payment_phone_e164=phone_normalized,
            auto_renew_enabled=True,
        )
        _attach_shops(sub, selected_shop_ids)

    txn = initiate_stk_push(
        owner=owner,
        subscription=sub,
        plan=plan,
        phone_number=phone_normalized,
        amount=amount,
        transaction_type=PaymentTransaction.TYPE_ACTIVATION,
    )
    return txn


def activate_subscription_from_successful_payment(txn: PaymentTransaction) -> ShopSubscription:
    """Called by callbacks.py after a verified successful payment."""
    sub = txn.subscription
    if sub is None:
        raise ValueError(f"Transaction {txn.id} has no linked subscription.")

    with transaction.atomic():
        sub.refresh_from_db()
        if txn.transaction_type in {
            PaymentTransaction.TYPE_ACTIVATION,
            PaymentTransaction.TYPE_UPGRADE,
        }:
            _retire_other_current_subscriptions(sub)
        _extend_subscription(sub)
        sub.status = ShopSubscription.STATUS_ACTIVE
        sub.grace_period_ends_at = None
        sub.cancelled_at = None
        sub.suspended_at = None
        sub.cancellation_requested_at = None
        sub.auto_renew_enabled = True
        sub.over_limit = False
        sub.mpesa_reference_last = txn.mpesa_receipt_number
        if txn.phone_number:
            sub.payment_phone_e164 = txn.phone_number
        sub.save()

    return sub


def request_upgrade(
    *,
    owner,
    target_plan_code: str,
    billing_interval: str,
    phone_number: str | None = None,
    selected_shop_ids: list[int] | None = None,
) -> PaymentTransaction:
    """Initiate upgrade payment.  Subscription activates on callback success."""
    from billing.services.plans import get_plan_by_code
    target_plan = get_plan_by_code(target_plan_code)

    current_sub = (
        ShopSubscription.objects.filter(owner=owner)
        .exclude(status__in=[ShopSubscription.STATUS_CANCELLED, ShopSubscription.STATUS_EXPIRED])
        .select_related("plan")
        .first()
    )
    if current_sub is None:
        raise ValueError("No active subscription to upgrade from.")

    phone_normalized = normalize_phone_number(
        phone_number or current_sub.payment_phone_e164 or ""
    )
    if not phone_normalized:
        raise ValueError("Phone number is required for upgrade payment.")

    shops_ids = selected_shop_ids or list(
        current_sub.subscription_shops.values_list("shop_id", flat=True)
    )
    validate_shop_selection_against_plan(target_plan, shops_ids)

    amount = target_plan.get_price(billing_interval)

    # Create a pending upgrade subscription
    with transaction.atomic():
        new_sub = ShopSubscription.objects.create(
            owner=owner,
            plan=target_plan,
            billing_interval=billing_interval,
            status=ShopSubscription.STATUS_TRIALING,
            starts_at=timezone.now(),
            payment_phone_e164=phone_normalized,
            auto_renew_enabled=True,
        )
        _attach_shops(new_sub, shops_ids)

    txn = initiate_stk_push(
        owner=owner,
        subscription=new_sub,
        plan=target_plan,
        phone_number=phone_normalized,
        amount=amount,
        transaction_type=PaymentTransaction.TYPE_UPGRADE,
    )
    return txn


def request_downgrade(*, owner, target_plan_code: str) -> ShopSubscription:
    """
    Schedule a downgrade to take effect at end of current period.
    Immediate data preservation — over_limit flag set if usage exceeds new plan.
    """
    from billing.services.plans import get_plan_by_code
    target_plan = get_plan_by_code(target_plan_code)

    current_sub = (
        ShopSubscription.objects.filter(
            owner=owner,
            status__in=[ShopSubscription.STATUS_ACTIVE, ShopSubscription.STATUS_TRIALING],
        )
        .select_related("plan")
        .first()
    )
    if current_sub is None:
        raise ValueError("No active subscription to downgrade from.")

    with transaction.atomic():
        current_sub.cancellation_requested_at = timezone.now()
        current_sub.notes = f"Downgrade to {target_plan.name} requested at period end."
        current_sub.save(update_fields=["cancellation_requested_at", "notes", "updated_at"])

    return current_sub


def cancel_at_period_end(owner) -> ShopSubscription:
    sub = (
        ShopSubscription.objects.filter(
            owner=owner,
            status__in=[ShopSubscription.STATUS_ACTIVE, ShopSubscription.STATUS_TRIALING],
        )
        .first()
    )
    if sub is None:
        raise ValueError("No active subscription to cancel.")

    sub.cancellation_requested_at = timezone.now()
    sub.auto_renew_enabled = False
    sub.save(update_fields=["cancellation_requested_at", "auto_renew_enabled", "updated_at"])
    return sub


def immediate_cancel(owner) -> ShopSubscription:
    sub = (
        ShopSubscription.objects.filter(owner=owner)
        .exclude(status__in=[ShopSubscription.STATUS_CANCELLED, ShopSubscription.STATUS_EXPIRED])
        .first()
    )
    if sub is None:
        raise ValueError("No subscription to cancel.")

    with transaction.atomic():
        sub.status = ShopSubscription.STATUS_CANCELLED
        sub.cancelled_at = timezone.now()
        sub.auto_renew_enabled = False
        sub.save(update_fields=["status", "cancelled_at", "auto_renew_enabled", "updated_at"])
    return sub


def move_to_grace_period(subscription: ShopSubscription, reason: str = "") -> ShopSubscription:
    """Move subscription to grace period, preserving all data."""
    grace_days = GRACE_PERIOD_DAYS
    with transaction.atomic():
        subscription.status = ShopSubscription.STATUS_GRACE
        subscription.grace_period_ends_at = timezone.now() + timedelta(days=grace_days)
        if reason:
            subscription.notes = (subscription.notes + f"\nGrace: {reason}").strip()
        subscription.save(update_fields=["status", "grace_period_ends_at", "notes", "updated_at"])
    return subscription


def suspend_if_grace_expired(subscription: ShopSubscription) -> bool:
    """Suspend subscription if grace period has ended.  Returns True if suspended."""
    if subscription.status != ShopSubscription.STATUS_GRACE:
        return False
    if subscription.grace_period_ends_at and timezone.now() > subscription.grace_period_ends_at:
        subscription.mark_suspended()
        over = _compute_over_limit(subscription)
        subscription.over_limit = over
        subscription.save(update_fields=["over_limit", "updated_at"])
        return True
    return False


def renew_subscription(subscription: ShopSubscription) -> PaymentTransaction | None:
    """Attempt an STK push renewal.  Returns the transaction or None if not applicable."""
    if not subscription.auto_renew_enabled:
        return None
    if subscription.plan.is_free:
        return None
    if not subscription.payment_phone_e164:
        logger.warning("Cannot renew sub %s: no payment phone on file.", subscription.id)
        return None

    amount = subscription.plan.get_price(subscription.billing_interval)

    txn = initiate_stk_push(
        owner=subscription.owner,
        subscription=subscription,
        plan=subscription.plan,
        phone_number=subscription.payment_phone_e164,
        amount=amount,
        transaction_type=PaymentTransaction.TYPE_RENEWAL,
    )
    subscription.last_renewal_attempt_at = timezone.now()
    subscription.save(update_fields=["last_renewal_attempt_at", "updated_at"])
    return txn
