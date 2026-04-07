"""Billing signals — auto-create Free subscription when a new shop is created."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from shops.models import Shop


@receiver(post_save, sender=Shop)
def ensure_owner_has_subscription(sender, instance: Shop, created: bool, **kwargs):
    """
    When a new shop is saved, ensure the owner has at least a Free subscription,
    and link this shop to it if there is capacity.
    """
    if not created:
        return

    from billing.services.subscriptions import get_or_create_free_subscription
    from billing.models import SubscriptionShop

    try:
        sub = get_or_create_free_subscription(instance.owner)
        # Attach this shop if within plan limit and not already attached
        current_count = sub.subscription_shops.count()
        if current_count < sub.plan.shops_limit:
            SubscriptionShop.objects.get_or_create(
                subscription=sub,
                shop=instance,
                defaults={"is_primary": current_count == 0},
            )
    except Exception:
        # Signals must never crash the save — log silently
        import logging
        logging.getLogger("payments").exception(
            "Failed to attach shop %s to subscription for owner %s",
            instance.id,
            instance.owner_id,
        )
