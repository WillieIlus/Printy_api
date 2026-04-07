"""Management command: seed_billing_plans — upsert the four canonical plans."""
from django.core.management.base import BaseCommand

from billing.services.plans import seed_plans


class Command(BaseCommand):
    help = "Seed or update the four canonical billing plans (Free, Biashara, Biashara Plus, Biashara Max)."

    def handle(self, *args, **options):
        self.stdout.write("Seeding billing plans...")
        seed_plans(stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("Done. All plans are up to date."))
