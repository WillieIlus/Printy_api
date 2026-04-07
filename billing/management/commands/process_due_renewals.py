"""Management command: process_due_renewals — fire STK pushes for all queued attempts."""
from django.core.management.base import BaseCommand

from billing.services.renewals import process_due_renewals, process_timed_out_renewals


class Command(BaseCommand):
    help = "Process queued renewal attempts and expire timed-out awaiting attempts."

    def handle(self, *args, **options):
        timed_out = process_timed_out_renewals()
        if timed_out:
            self.stdout.write(f"  Marked {timed_out} attempt(s) as timed out.")

        processed = process_due_renewals()
        self.stdout.write(self.style.SUCCESS(f"Processed {processed} renewal attempt(s)."))
