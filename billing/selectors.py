"""Billing selectors — read-only query helpers."""
from __future__ import annotations

from datetime import date

from django.db.models import QuerySet
from django.utils import timezone

from billing.models import (
    Plan,
    PaymentTransaction,
    ShopSubscription,
    UsageCounter,
)


def get_active_subscription_for_owner(owner) -> ShopSubscription | None:
    """
    Return the most-relevant subscription for an owner.
    Priority: active > trialing > grace > past_due.
    Falls back to creating a Free subscription if none exists.
    """
    STATUS_PRIORITY = [
        ShopSubscription.STATUS_ACTIVE,
        ShopSubscription.STATUS_TRIALING,
        ShopSubscription.STATUS_GRACE,
        ShopSubscription.STATUS_PAST_DUE,
    ]
    subs = (
        ShopSubscription.objects.filter(
            owner=owner,
            status__in=STATUS_PRIORITY,
        )
        .select_related("plan")
        .order_by("id")
    )
    for status in STATUS_PRIORITY:
        match = next((s for s in subs if s.status == status), None)
        if match:
            return match

    # None found — lazily create a Free subscription
    from billing.services.subscriptions import get_or_create_free_subscription
    return get_or_create_free_subscription(owner)


def get_subscription_detail(owner) -> ShopSubscription | None:
    return (
        ShopSubscription.objects.filter(owner=owner)
        .select_related("plan")
        .prefetch_related("subscription_shops__shop")
        .exclude(status__in=[ShopSubscription.STATUS_CANCELLED, ShopSubscription.STATUS_EXPIRED])
        .order_by("-created_at")
        .first()
    )


def get_public_plans() -> QuerySet[Plan]:
    return Plan.objects.filter(is_active=True).order_by("sort_order")


def get_owner_transactions(owner) -> QuerySet[PaymentTransaction]:
    return PaymentTransaction.objects.filter(owner=owner).select_related("plan").order_by("-created_at")


def get_or_create_usage_counter(owner, subscription) -> UsageCounter:
    today = timezone.now().date()
    month_start = today.replace(day=1)
    counter, _ = UsageCounter.objects.get_or_create(
        owner=owner,
        month=month_start,
        defaults={"subscription": subscription},
    )
    return counter
