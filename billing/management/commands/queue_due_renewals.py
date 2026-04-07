"""Management command: queue_due_renewals — create RenewalAttempt rows for due subscriptions."""
from django.core.management.base import BaseCommand

from billing.services.renewals import queue_due_renewals


class Command(BaseCommand):
    help = "Queue renewal attempts for all subscriptions whose renews_at has passed."

    def handle(self, *args, **options):
        created = queue_due_renewals()
        self.stdout.write(self.style.SUCCESS(f"Queued {created} new renewal attempt(s)."))
