from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.utils import timezone

from quotes.choices import QuoteStatus, ShopQuoteStatus
from quotes.messaging import create_quote_message
from quotes.models import QuoteRequestMessage


DEFAULT_QUOTE_EXPIRY_HOURS = 48
DEFAULT_PARTNER_MARKUP_MIN = Decimal("0.05")
DEFAULT_PARTNER_MARKUP_MAX = Decimal("2.00")
DEFAULT_PARTNER_MARKUP_DEFAULT = Decimal("0.30")
DEFAULT_PARTNER_MARKUP_WARNING = Decimal("1.00")


def _money(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def get_quote_expiry_hours() -> int:
    try:
        return int(getattr(settings, "QUOTE_EXPIRY_HOURS", DEFAULT_QUOTE_EXPIRY_HOURS))
    except Exception:
        return DEFAULT_QUOTE_EXPIRY_HOURS


def calculate_quote_expiry(*, sent_at=None):
    base = sent_at or timezone.now()
    return base + timedelta(hours=get_quote_expiry_hours())


def get_partner_markup_min_rate() -> Decimal:
    return _money(getattr(settings, "PARTNER_MARKUP_MIN", DEFAULT_PARTNER_MARKUP_MIN), default=str(DEFAULT_PARTNER_MARKUP_MIN))


def get_partner_markup_max_rate() -> Decimal:
    return _money(getattr(settings, "PARTNER_MARKUP_MAX", DEFAULT_PARTNER_MARKUP_MAX), default=str(DEFAULT_PARTNER_MARKUP_MAX))


def get_partner_markup_default_rate() -> Decimal:
    return _money(getattr(settings, "PARTNER_MARKUP_DEFAULT", DEFAULT_PARTNER_MARKUP_DEFAULT), default=str(DEFAULT_PARTNER_MARKUP_DEFAULT))


def get_partner_markup_warning_rate() -> Decimal:
    return _money(getattr(settings, "PARTNER_MARKUP_WARNING", DEFAULT_PARTNER_MARKUP_WARNING), default=str(DEFAULT_PARTNER_MARKUP_WARNING))


def markup_rate_from_amount(*, base_price: Decimal | int | float | str, markup_amount: Decimal | int | float | str) -> Decimal:
    production_amount = _money(base_price)
    markup = _money(markup_amount)
    if production_amount <= 0:
        raise ValueError("Production price is not available yet for the selected shop.")
    return (markup / production_amount).quantize(Decimal("0.0001"))


def validate_partner_markup_amount(*, base_price: Decimal | int | float | str, markup_amount: Decimal | int | float | str) -> Decimal:
    rate = markup_rate_from_amount(base_price=base_price, markup_amount=markup_amount)
    min_rate = get_partner_markup_min_rate()
    max_rate = get_partner_markup_max_rate()
    if rate < min_rate:
        raise ValueError(f"Markup cannot be below {int(min_rate * Decimal('100'))}%.")
    if rate > max_rate:
        raise ValueError(f"Markup cannot exceed {int(max_rate * Decimal('100'))}%.")
    return rate


def build_partner_markup_warning(*, base_price: Decimal | int | float | str, markup_amount: Decimal | int | float | str) -> str:
    rate = markup_rate_from_amount(base_price=base_price, markup_amount=markup_amount)
    warning_rate = get_partner_markup_warning_rate()
    if rate > warning_rate:
        return "Your client will pay more than double production cost. Are you sure?"
    return ""


def expire_shop_quote(*, shop_quote, now=None, notify_manager: bool = True) -> bool:
    current_time = now or timezone.now()
    if not getattr(shop_quote, "expires_at", None) or shop_quote.expires_at > current_time:
        return False
    if shop_quote.status == ShopQuoteStatus.EXPIRED:
        return False
    if shop_quote.status not in {ShopQuoteStatus.SENT, ShopQuoteStatus.REVISED, ShopQuoteStatus.MODIFIED}:
        return False

    shop_quote.status = ShopQuoteStatus.EXPIRED
    if getattr(shop_quote, "client_quote_status", "") == "sent":
        shop_quote.client_quote_status = "expired"
    shop_quote.save(update_fields=["status", "client_quote_status", "updated_at"])

    quote_request = shop_quote.quote_request
    if quote_request.status not in {QuoteStatus.ACCEPTED, QuoteStatus.CANCELLED, QuoteStatus.CLOSED, QuoteStatus.REJECTED}:
        latest_response = quote_request.get_latest_response()
        if latest_response and latest_response.id == shop_quote.id:
            quote_request.status = QuoteStatus.EXPIRED
            quote_request.save(update_fields=["status", "updated_at"])

    if notify_manager:
        recipient = getattr(shop_quote, "sent_to_client_by", None) or getattr(shop_quote, "created_by", None)
        if recipient is not None:
            client_label = quote_request.customer_name or "client"
            create_quote_message(
                quote_request=quote_request,
                shop_quote=shop_quote,
                sender=None,
                recipient=recipient,
                recipient_email=getattr(recipient, "email", "") or "",
                sender_role=QuoteRequestMessage.SenderRole.SYSTEM,
                recipient_role=QuoteRequestMessage.RecipientRole.ADMIN,
                message_kind=QuoteRequestMessage.MessageKind.STATUS,
                message_type=QuoteRequestMessage.MessageType.SYSTEM_NOTICE,
                direction=QuoteRequestMessage.Direction.OUTBOUND,
                subject="Quote expired in Printy",
                body=f"Your quote to {client_label} expired.",
                metadata={"quote_status": ShopQuoteStatus.EXPIRED},
                send_email_copy=bool(getattr(recipient, "email", "")),
                create_failure_notice=True,
            )

    return True
