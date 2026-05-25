from django.core.management.base import BaseCommand
from django.utils import timezone

from quotes.choices import ShopQuoteStatus
from quotes.guardrails import expire_shop_quote
from quotes.models import ShopQuote


class Command(BaseCommand):
    help = "Marks expired client-facing quotes as expired."

    def handle(self, *args, **options):
        now = timezone.now()
        expired_count = 0
        queryset = ShopQuote.objects.select_related("quote_request", "created_by", "sent_to_client_by").filter(
            status__in=[ShopQuoteStatus.SENT, ShopQuoteStatus.REVISED, ShopQuoteStatus.MODIFIED],
            expires_at__isnull=False,
            expires_at__lt=now,
        )
        for shop_quote in queryset.iterator():
            if expire_shop_quote(shop_quote=shop_quote, now=now):
                expired_count += 1

        self.stdout.write(self.style.SUCCESS(f"Expired {expired_count} quote(s)."))
