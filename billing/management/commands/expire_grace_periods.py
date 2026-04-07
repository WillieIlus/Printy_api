"""Management command: expire_grace_periods — suspend subscriptions whose grace period ended."""
from django.core.management.base import BaseCommand

from billing.services.renewals import expire_grace_periods


class Command(BaseCommand):
    help = "Suspend subscriptions whose grace period has expired."

    def handle(self, *args, **options):
        count = expire_grace_periods()
        self.stdout.write(self.style.SUCCESS(f"Suspended {count} subscription(s) with expired grace periods."))
