"""Entitlement service — enforces plan limits across Printy.ke resources."""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from django.db.models import Count
from django.utils import timezone

from billing.models import Plan, ShopSubscription, UsageCounter
from billing.selectors import get_active_subscription_for_owner

if TYPE_CHECKING:
    from accounts.models import User
    from shops.models import Shop


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ok(current: int | None, limit: int | None, resource: str) -> dict:
    return {"allowed": True, "reason_code": "ok", "message": "", "current": current, "limit": limit}


def _denied(reason_code: str, message: str, current: int | None, limit: int | None) -> dict:
    return {"allowed": False, "reason_code": reason_code, "message": message, "current": current, "limit": limit}


def _suspended_response(resource: str) -> dict:
    return _denied(
        "subscription_suspended",
        f"Your subscription is suspended. Please reactivate to add {resource}.",
        None,
        None,
    )


def _get_subscription(owner) -> ShopSubscription | None:
    """Return the governing subscription for entitlement checks, including suspended ones."""
    priority = [
        ShopSubscription.STATUS_ACTIVE,
        ShopSubscription.STATUS_TRIALING,
        ShopSubscription.STATUS_GRACE,
        ShopSubscription.STATUS_PAST_DUE,
        ShopSubscription.STATUS_SUSPENDED,
    ]
    for s in priority:
        sub = (
            ShopSubscription.objects.filter(owner=owner, status=s)
            .select_related("plan")
            .first()
        )
        if sub:
            return sub
    from billing.services.subscriptions import get_or_create_free_subscription
    return get_or_create_free_subscription(owner)


def _get_owner_shop_ids(owner) -> list[int]:
    from shops.models import Shop as ShopModel
    return list(ShopModel.objects.filter(owner=owner).values_list("id", flat=True))


def _count_owner_shops(owner) -> int:
    from shops.models import Shop as ShopModel
    return ShopModel.objects.filter(owner=owner).count()


def _count_machines_for_owner(owner) -> int:
    from inventory.models import Machine
    shop_ids = _get_owner_shop_ids(owner)
    return Machine.objects.filter(shop_id__in=shop_ids, is_active=True).count()


def _count_active_products_for_owner(owner) -> int:
    from catalog.models import Product
    shop_ids = _get_owner_shop_ids(owner)
    return Product.objects.filter(shop_id__in=shop_ids, is_active=True).count()


def _count_active_users_for_owner(owner) -> int:
    """Count distinct staff/manager members across all owner's shops."""
    from shops.models import ShopMembership
    shop_ids = _get_owner_shop_ids(owner)
    # +1 for the owner themselves
    return ShopMembership.objects.filter(shop_id__in=shop_ids, is_active=True).values("user_id").distinct().count() + 1


def _count_quotes_this_month(owner) -> int:
    """Count ShopQuotes created by shops belonging to owner in the current calendar month."""
    from quotes.models import ShopQuote
    shop_ids = _get_owner_shop_ids(owner)
    now = timezone.now()
    return ShopQuote.objects.filter(
        quote_request__shop_id__in=shop_ids,
        created_at__year=now.year,
        created_at__month=now.month,
    ).count()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_plan_limits(subscription: ShopSubscription) -> dict:
    plan = subscription.plan
    return {
        "shops_limit": plan.shops_limit,
        "machines_limit": plan.machines_limit,
        "products_limit": plan.products_limit,
        "quotes_per_month_limit": plan.quotes_per_month_limit,
        "users_limit": plan.users_limit,
    }


def get_current_usage(owner) -> dict:
    return {
        "shops": _count_owner_shops(owner),
        "machines": _count_machines_for_owner(owner),
        "active_products": _count_active_products_for_owner(owner),
        "team_members": _count_active_users_for_owner(owner),
        "quotes_this_month": _count_quotes_this_month(owner),
    }


def check_can_create_shop(owner) -> dict:
    sub = _get_subscription(owner)
    if not sub:
        return _denied("no_subscription", "No active subscription found.", 0, 1)

    if sub.status == ShopSubscription.STATUS_SUSPENDED:
        return _suspended_response("shops")

    current = _count_owner_shops(owner)
    limit = sub.plan.shops_limit

    if current >= limit:
        return _denied(
            "shop_limit_reached",
            f"Your {sub.plan.name} plan allows {limit} shop(s). You already have {current}.",
            current,
            limit,
        )
    return _ok(current, limit, "shop")


def check_can_add_shop_to_subscription(subscription: ShopSubscription) -> dict:
    current = subscription.subscription_shops.count()
    limit = subscription.plan.shops_limit
    if current >= limit:
        return _denied(
            "shop_limit_reached",
            f"Plan allows {limit} shop(s); subscription already has {current}.",
            current,
            limit,
        )
    return _ok(current, limit, "subscription shop")


def check_can_create_machine(shop: Shop) -> dict:
    owner = shop.owner
    sub = _get_subscription(owner)
    if not sub:
        return _denied("no_subscription", "No active subscription found.", 0, 1)

    if sub.status == ShopSubscription.STATUS_SUSPENDED:
        return _suspended_response("machines")

    limit = sub.plan.machines_limit
    if limit is None:
        current = _count_machines_for_owner(owner)
        return _ok(current, None, "machine")

    current = _count_machines_for_owner(owner)
    if current >= limit:
        return _denied(
            "machine_limit_reached",
            f"Your {sub.plan.name} plan allows {limit} machine(s). You already have {current}.",
            current,
            limit,
        )
    return _ok(current, limit, "machine")


def check_can_create_product(shop: Shop) -> dict:
    owner = shop.owner
    sub = _get_subscription(owner)
    if not sub:
        return _denied("no_subscription", "No active subscription found.", 0, 1)

    if sub.status == ShopSubscription.STATUS_SUSPENDED:
        return _suspended_response("products")

    limit = sub.plan.products_limit
    if limit is None:
        current = _count_active_products_for_owner(owner)
        return _ok(current, None, "product")

    current = _count_active_products_for_owner(owner)
    if current >= limit:
        return _denied(
            "product_limit_reached",
            f"Your {sub.plan.name} plan allows {limit} product(s). You currently have {current} active.",
            current,
            limit,
        )
    return _ok(current, limit, "product")


def check_can_create_quote(owner) -> dict:
    sub = _get_subscription(owner)
    if not sub:
        return _denied("no_subscription", "No active subscription found.", 0, 0)

    if sub.status == ShopSubscription.STATUS_SUSPENDED:
        return _suspended_response("quotes")

    limit = sub.plan.quotes_per_month_limit
    if limit is None:
        current = _count_quotes_this_month(owner)
        return _ok(current, None, "quote")

    current = _count_quotes_this_month(owner)
    if current >= limit:
        return _denied(
            "quote_limit_reached",
            f"Your {sub.plan.name} plan allows {limit} quotes per month. You have used {current} this month.",
            current,
            limit,
        )
    return _ok(current, limit, "quote")


def check_can_add_user(shop: Shop) -> dict:
    owner = shop.owner
    sub = _get_subscription(owner)
    if not sub:
        return _denied("no_subscription", "No active subscription found.", 0, 1)

    if sub.status == ShopSubscription.STATUS_SUSPENDED:
        return _suspended_response("team members")

    current = _count_active_users_for_owner(owner)
    limit = sub.plan.users_limit

    if current >= limit:
        return _denied(
            "user_limit_reached",
            f"Your {sub.plan.name} plan allows {limit} team member(s). You already have {current}.",
            current,
            limit,
        )
    return _ok(current, limit, "team member")
