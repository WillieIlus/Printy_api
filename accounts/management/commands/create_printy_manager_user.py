from django.core.management.base import BaseCommand

from accounts.services.system_accounts import ensure_printy_manager_user


class Command(BaseCommand):
    help = "Create or update the Printy fallback manager system user."

    def handle(self, *args, **options):
        user, profile, created = ensure_printy_manager_user()
        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} Printy manager user #{user.id} ({user.email})."))
        self.stdout.write(f"PRINTY_MANAGER_USER_ID={user.id}")
        self.stdout.write(f"default_markup_rate={profile.default_markup_rate}")
