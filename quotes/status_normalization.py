"""Normalize legacy quote statuses to stable frontend-facing statuses."""

from quotes.choices import QuoteDraftStatus, QuoteStatus, ShopQuoteStatus


def normalize_quote_draft_status(raw_status: str | None, *, has_shop=False, has_request_details=False, has_pricing=False) -> str:
    if raw_status == QuoteDraftStatus.SENT:
        return "sent"
    if raw_status == QuoteDraftStatus.ARCHIVED:
        return "abandoned"
    if raw_status == QuoteDraftStatus.DRAFT and (has_shop or has_request_details or has_pricing):
        return "ready_to_send"
    return "draft"


def quote_draft_status_label(status: str) -> str:
    return {
        "draft": "Draft",
        "ready_to_send": "Ready to send",
        "sent": "Sent",
        "abandoned": "Abandoned",
    }.get(status, "Draft")


def normalize_quote_request_status(raw_status: str | None) -> str:
    return {
        QuoteStatus.DRAFT: "pending",
        QuoteStatus.SUBMITTED: "sent",
        QuoteStatus.AWAITING_SHOP_ACTION: "pending",
        QuoteStatus.ACCEPTED: "pending",
        QuoteStatus.AWAITING_CLIENT_REPLY: "needs_confirmation",
        QuoteStatus.VIEWED: "viewed",
        QuoteStatus.QUOTED: "responded",
        QuoteStatus.REJECTED: "rejected",
        QuoteStatus.EXPIRED: "expired",
        QuoteStatus.CLOSED: "accepted",
        QuoteStatus.CANCELLED: "cancelled",
    }.get(raw_status or "", "pending")


def quote_request_status_label(status: str) -> str:
    return {
        "pending": "Pending",
        "sent": "Sent",
        "viewed": "Viewed",
        "needs_confirmation": "Needs confirmation",
        "responded": "Responded",
        "accepted": "Accepted",
        "rejected": "Rejected",
        "expired": "Expired",
        "cancelled": "Cancelled",
    }.get(status, "Pending")


def normalize_quote_response_status(raw_status: str | None) -> str:
    return {
        ShopQuoteStatus.PENDING: "draft",
        ShopQuoteStatus.SENT: "sent",
        ShopQuoteStatus.MODIFIED: "modified",
        "revised": "modified",
        ShopQuoteStatus.ACCEPTED: "accepted",
        ShopQuoteStatus.REJECTED: "rejected",
        "declined": "rejected",
        ShopQuoteStatus.EXPIRED: "expired",
    }.get(raw_status or "", "draft")


def quote_response_status_label(status: str) -> str:
    return {
        "draft": "Draft",
        "sent": "Sent",
        "modified": "Modified",
        "accepted": "Accepted",
        "rejected": "Rejected",
        "expired": "Expired",
    }.get(status, "Draft")


def denormalize_quote_response_status(status: str) -> str:
    return {
        "draft": ShopQuoteStatus.PENDING,
        "sent": ShopQuoteStatus.SENT,
        "modified": ShopQuoteStatus.MODIFIED,
        "accepted": ShopQuoteStatus.ACCEPTED,
        "rejected": ShopQuoteStatus.REJECTED,
        "expired": ShopQuoteStatus.EXPIRED,
    }.get(status, status)
