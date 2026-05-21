"""Canonical M-Pesa status vocabulary shared across billing and managed jobs."""
from __future__ import annotations


CANONICAL_INITIATED = "initiated"
CANONICAL_PENDING = "pending"
CANONICAL_PAID = "paid"
CANONICAL_FAILED = "failed"
CANONICAL_CANCELLED = "cancelled"
CANONICAL_NEEDS_REVIEW = "needs_review"

CANONICAL_LABELS = {
    CANONICAL_INITIATED: "Initiated",
    CANONICAL_PENDING: "Pending",
    CANONICAL_PAID: "Paid",
    CANONICAL_FAILED: "Failed",
    CANONICAL_CANCELLED: "Cancelled",
    CANONICAL_NEEDS_REVIEW: "Needs review",
}


def canonical_label(value: str) -> str:
    return CANONICAL_LABELS.get(value, value.replace("_", " ").title())


def canonicalize_billing_status(*, status: str, result_desc: str = "") -> str:
    review_markers = {
        "duplicate receipt number",
        "success callback missing mpesareceiptnumber",
        "amount mismatch",
        "manual review",
    }
    normalized_desc = (result_desc or "").strip().lower()
    if normalized_desc in review_markers:
        return CANONICAL_NEEDS_REVIEW
    if status == "initiated":
        return CANONICAL_INITIATED
    if status in {"pending", "processing"}:
        return CANONICAL_PENDING
    if status in {"paid", "success"}:
        return CANONICAL_PAID
    if status == "cancelled" or status == "timed_out":
        return CANONICAL_CANCELLED
    if status == "failed":
        return CANONICAL_FAILED
    return CANONICAL_NEEDS_REVIEW


def canonicalize_job_status(*, payment_status: str, reconciliation_status: str = "") -> str:
    review_statuses = {
        "amount_mismatch",
        "manual_review",
        "unknown_reference",
        "duplicate_callback",
        "duplicate_receipt",
    }
    if reconciliation_status in review_statuses:
        return CANONICAL_NEEDS_REVIEW
    if payment_status in {"initiated", "stk_push_sent"}:
        return CANONICAL_INITIATED
    if payment_status in {"pending", "manual_payment_pending", "confirmation_pending"}:
        return CANONICAL_PENDING
    if payment_status in {"paid", "confirmed"}:
        return CANONICAL_PAID
    if payment_status == "failed":
        return CANONICAL_FAILED
    if payment_status in {"cancelled", "refunded"}:
        return CANONICAL_CANCELLED
    return CANONICAL_NEEDS_REVIEW
