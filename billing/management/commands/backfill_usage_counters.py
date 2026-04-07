"""Management command: backfill_usage_counters — compute/refresh current month usage snapshots."""
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from billing.models import UsageCounter
from billing.selectors import get_active_subscription_for_owner
from billing.services.entitlements import get_current_usage

User = get_user_model()


class Command(BaseCommand):
    help = "Backfill or refresh UsageCounter snapshots for the current month for all shop owners."

    def add_arguments(self, parser):
        parser.add_argument(
            "--month",
            help="Month in YYYY-MM format. Defaults to current month.",
            default=None,
        )

    def handle(self, *args, **options):
        month_str = options.get("month")
        if month_str:
            year, mo = map(int, month_str.split("-"))
            month_start = date(year, mo, 1)
        else:
            today = timezone.now().date()
            month_start = today.replace(day=1)

        from shops.models import Shop
        owner_ids = Shop.objects.values_list("owner_id", flat=True).distinct()
        owners = User.objects.filter(id__in=owner_ids)

        updated = 0
        for owner in owners:
            sub = get_active_subscription_for_owner(owner)
            usage = get_current_usage(owner)

            counter, _ = UsageCounter.objects.update_or_create(
                owner=owner,
                month=month_start,
                defaults={
                    "subscription": sub,
                    "quotes_created_count": usage["quotes_this_month"],
                    "active_products_count_snapshot": usage["active_products"],
                    "active_users_count_snapshot": usage["team_members"],
                    "active_machines_count_snapshot": usage["machines"],
                    "shops_count_snapshot": usage["shops"],
                },
            )
            updated += 1

        self.stdout.write(
            self.style.SUCCESS(f"Updated UsageCounter for {updated} owner(s) for {month_start}.")
        )
