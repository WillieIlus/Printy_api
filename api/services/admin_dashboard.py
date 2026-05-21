from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import User
from accounts.services.roles import CANONICAL_SUPER_ADMIN_ROLE, resolve_user_roles
from billing.models import PaymentTransaction
from common.models import AnalyticsEvent
from common.payment_constants import PaymentStatus
from jobs.choices import (
    JobAssignmentStatus,
    JobPaymentStatus,
    JobSettlementStatus,
    ManagedJobPaymentStatus,
    ManagedJobStatus,
)
from jobs.models import JobAssignment, JobPayment, JobSettlementSplit, ManagedJob
from quotes.models import QuoteDraft, QuoteRequest, ShopQuote
from shops.models import Shop

ZERO_DECIMAL = Decimal("0.00")


@dataclass(frozen=True)
class ComparisonWindow:
    key: str
    label: str
    current_start: datetime
    current_end: datetime
    previous_start: datetime
    previous_end: datetime


def safe_percent_change(current: Decimal, previous: Decimal) -> Decimal | None:
    if previous == 0:
        return Decimal("0") if current == 0 else None
    return ((current - previous) / previous) * Decimal("100")


def calculate_change(current: Decimal, previous: Decimal) -> dict[str, object]:
    absolute_change = current - previous
    percent_change = safe_percent_change(current, previous)
    if absolute_change > 0:
        trend = "up"
    elif absolute_change < 0:
        trend = "down"
    else:
        trend = "flat"

    helper_text = "No change from the previous period."
    if previous == 0 and current > 0:
        helper_text = "New activity compared with an empty prior period."
    elif trend == "up":
        helper_text = "Up versus the previous period."
    elif trend == "down":
        helper_text = "Down versus the previous period."

    return {
        "absolute_change": _serialize_number(absolute_change),
        "percent_change": _serialize_number(percent_change) if percent_change is not None else None,
        "trend": trend,
        "helper_text": helper_text,
    }


def money_sum(queryset, field: str, *, fallback_field: str | None = None) -> Decimal:
    if fallback_field:
        aggregate = queryset.aggregate(
            total=Sum(
                Coalesce(field, fallback_field, Value(ZERO_DECIMAL), output_field=DecimalField(max_digits=14, decimal_places=2))
            )
        )
        return aggregate["total"] or ZERO_DECIMAL
    aggregate = queryset.aggregate(total=Sum(Coalesce(field, Value(ZERO_DECIMAL), output_field=DecimalField(max_digits=14, decimal_places=2))))
    return aggregate["total"] or ZERO_DECIMAL


def status_count(queryset, status_field: str, *statuses: str) -> int:
    if not statuses:
        return 0
    return queryset.filter(**{f"{status_field}__in": statuses}).count()


def _count_in_window(queryset, datetime_field: str, start, end) -> int:
    return queryset.filter(**{f"{datetime_field}__gte": start, f"{datetime_field}__lt": end}).count()


def _sum_in_window(queryset, datetime_field: str, amount_field: str, start, end, *, fallback_field: str | None = None) -> Decimal:
    scoped = queryset.filter(**{f"{datetime_field}__gte": start, f"{datetime_field}__lt": end})
    return money_sum(scoped, amount_field, fallback_field=fallback_field)


def get_time_window_comparison(
    queryset,
    datetime_field: str,
    current_start,
    current_end,
    previous_start,
    previous_end,
    *,
    amount_field: str | None = None,
    fallback_amount_field: str | None = None,
) -> dict[str, object]:
    if amount_field:
        current_value = _sum_in_window(
            queryset,
            datetime_field,
            amount_field,
            current_start,
            current_end,
            fallback_field=fallback_amount_field,
        )
        previous_value = _sum_in_window(
            queryset,
            datetime_field,
            amount_field,
            previous_start,
            previous_end,
            fallback_field=fallback_amount_field,
        )
    else:
        current_value = Decimal(_count_in_window(queryset, datetime_field, current_start, current_end))
        previous_value = Decimal(_count_in_window(queryset, datetime_field, previous_start, previous_end))

    return {
        "current_value": _serialize_number(current_value),
        "previous_value": _serialize_number(previous_value),
        **calculate_change(current_value, previous_value),
    }


def _local_now():
    return timezone.localtime(timezone.now())


def _build_comparison_windows(now):
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    last_month_end = month_start
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)

    windows = [
        ComparisonWindow("hour", "This hour vs last hour", hour_start, now, hour_start - timedelta(hours=1), hour_start),
        ComparisonWindow("three_hours", "Last 3h vs previous 3h", now - timedelta(hours=3), now, now - timedelta(hours=6), now - timedelta(hours=3)),
        ComparisonWindow("six_hours", "Last 6h vs previous 6h", now - timedelta(hours=6), now, now - timedelta(hours=12), now - timedelta(hours=6)),
        ComparisonWindow("twelve_hours", "Last 12h vs previous 12h", now - timedelta(hours=12), now, now - timedelta(hours=24), now - timedelta(hours=12)),
        ComparisonWindow("day", "Today vs yesterday", today_start, now, today_start - timedelta(days=1), today_start),
        ComparisonWindow("week", "This week vs last week", week_start, now, week_start - timedelta(days=7), week_start),
        ComparisonWindow("month", "This month vs last month", month_start, now, last_month_start, last_month_end),
    ]
    return windows


def _comparison_payload(label: str, queryset, datetime_field: str, *, amount_field: str | None = None, fallback_amount_field: str | None = None):
    payload: dict[str, object] = {"label": label, "comparisons": {}}
    for window in _build_comparison_windows(_local_now()):
        payload["comparisons"][window.key] = {
            "label": window.label,
            **get_time_window_comparison(
                queryset,
                datetime_field,
                window.current_start,
                window.current_end,
                window.previous_start,
                window.previous_end,
                amount_field=amount_field,
                fallback_amount_field=fallback_amount_field,
            ),
        }
    return payload


def _unavailable_metric(label: str, reason: str) -> dict[str, object]:
    comparisons = {}
    for window in _build_comparison_windows(_local_now()):
        comparisons[window.key] = {
            "label": window.label,
            "current_value": None,
            "previous_value": None,
            "absolute_change": None,
            "percent_change": None,
            "trend": "flat",
            "helper_text": reason,
            "unavailable_reason": reason,
        }
    return {"label": label, "comparisons": comparisons, "unavailable_reason": reason}


def _serialize_number(value: Decimal | int | None) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        if value == value.to_integral():
            return int(value)
        return f"{value.quantize(Decimal('0.01'))}"
    return value


def _mask_phone(phone: str) -> str:
    normalized = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(normalized) < 4:
        return ""
    return f"***{normalized[-4:]}"


def _role_bucket_counts() -> dict[str, int]:
    counts = {
        CANONICAL_SUPER_ADMIN_ROLE: 0,
        "client": 0,
        "partner": 0,
        "production": 0,
    }
    for user in User.objects.all().only("id", "role", "is_staff", "is_superuser", "partner_profile_enabled"):
        primary_role = next(iter(resolve_user_roles(user)), "client")
        counts[primary_role] = counts.get(primary_role, 0) + 1
    return counts


def build_admin_dashboard_payload() -> dict[str, object]:
    now = _local_now()
    users = User.objects.all()
    quote_requests = QuoteRequest.objects.all()
    shop_quotes = ShopQuote.objects.all()
    managed_jobs = ManagedJob.objects.all()
    job_payments = JobPayment.objects.all()
    billing_transactions = PaymentTransaction.objects.all()
    settlements = JobSettlementSplit.objects.all()
    shops = Shop.objects.all()
    drafts = QuoteDraft.objects.all()
    assignments = JobAssignment.objects.filter(reassigned_from__isnull=True)
    analytics_events = AnalyticsEvent.objects.all()

    role_counts = _role_bucket_counts()
    pending_callback_filter = Q(payment_status__in=[JobPaymentStatus.INITIATED, JobPaymentStatus.PENDING]) & Q(confirmed_at__isnull=True)
    billing_pending_callback_filter = Q(status__in=[PaymentStatus.INITIATED, PaymentStatus.PENDING]) & Q(callback_received_at__isnull=True)
    confirmed_job_payments = job_payments.filter(payment_status=JobPaymentStatus.PAID, confirmed_at__isnull=False)
    confirmed_billing_payments = billing_transactions.filter(status=PaymentStatus.PAID)
    production_payouts_pending = settlements.filter(status__in=[JobSettlementStatus.PENDING, JobSettlementStatus.HELD, JobSettlementStatus.RELEASE_READY])
    broker_payouts_pending = production_payouts_pending.filter(partner_commission__gt=0)

    total_collected = money_sum(confirmed_job_payments, "received_amount", fallback_field="amount") + money_sum(
        confirmed_billing_payments, "amount"
    )

    latest_quotes = [
        {
            "id": quote.id,
            "reference": quote.request_reference or f"QR-{quote.id}",
            "status": quote.status,
            "customer_name": quote.customer_name or quote.customer_email or "Client",
            "shop_name": getattr(quote.shop, "name", "") or "Shop",
            "created_at": timezone.localtime(quote.created_at).isoformat(),
        }
        for quote in quote_requests.select_related("shop").order_by("-created_at")[:8]
    ]
    latest_jobs = [
        {
            "id": job.id,
            "reference": job.managed_reference or f"MJ-{job.id}",
            "status": job.status,
            "payment_status": job.payment_status,
            "client_total": _serialize_number(job.client_total),
            "assigned_shop_name": getattr(job.assigned_shop, "name", "") or "Awaiting assignment",
            "created_at": timezone.localtime(job.created_at).isoformat(),
        }
        for job in managed_jobs.select_related("assigned_shop").order_by("-created_at")[:8]
    ]
    latest_payments = [
        {
            "id": payment.id,
            "source": "job",
            "reference": payment.account_reference or payment.checkout_request_id or f"PAY-{payment.id}",
            "amount": _serialize_number(payment.received_amount or payment.amount),
            "status": payment.payment_status,
            "receipt_number": payment.mpesa_receipt_number or "",
            "phone": _mask_phone(payment.payer_phone),
            "created_at": timezone.localtime(payment.created_at).isoformat(),
        }
        for payment in job_payments.order_by("-created_at")[:8]
    ]
    shops_needing_attention = [
        {
            "id": shop.id,
            "name": shop.name,
            "pricing_ready": shop.pricing_ready,
            "public_match_ready": shop.public_match_ready,
            "active_jobs": shop.managed_jobs.exclude(status__in=[ManagedJobStatus.COMPLETED, ManagedJobStatus.CANCELLED]).count(),
            "issue": "Missing pricing" if not shop.pricing_ready else "Pending jobs",
        }
        for shop in shops.order_by("pricing_ready", "public_match_ready", "name")[:8]
    ]
    recent_users = [
        {
            "id": user.id,
            "email": user.email,
            "name": user.name or user.email,
            "primary_role": next(iter(resolve_user_roles(user)), "client"),
            "last_login": timezone.localtime(user.last_login).isoformat() if user.last_login else None,
            "date_joined": timezone.localtime(user.date_joined).isoformat() if user.date_joined else None,
        }
        for user in users.order_by("-date_joined")[:8]
    ]

    return {
        "role": CANONICAL_SUPER_ADMIN_ROLE,
        "generated_at": now.isoformat(),
        "timezone": timezone.get_current_timezone_name(),
        "home_route": "/dashboard/admin",
        "kpis": [
            {
                "key": "gross_client_revenue",
                "label": "Gross client revenue",
                "value": _serialize_number(money_sum(managed_jobs, "client_total")),
                "comparison_key": "day",
                "metric_key": "payment_amount_collected",
                "helper_text": "Managed-job client totals currently modeled in the platform.",
            },
            {
                "key": "confirmed_mpesa_payments",
                "label": "Confirmed MPESA payments",
                "value": confirmed_job_payments.count() + confirmed_billing_payments.count(),
                "comparison_key": "day",
                "metric_key": "confirmed_payments",
                "helper_text": "Includes job payments plus subscription transactions.",
            },
            {
                "key": "quote_requests",
                "label": "Quote requests",
                "value": quote_requests.count(),
                "comparison_key": "day",
                "metric_key": "quote_requests",
                "helper_text": "Platform-wide quote intake.",
            },
            {
                "key": "active_jobs",
                "label": "Active jobs",
                "value": managed_jobs.exclude(status__in=[ManagedJobStatus.COMPLETED, ManagedJobStatus.CANCELLED]).count(),
                "comparison_key": "day",
                "metric_key": "jobs_created",
                "helper_text": "Managed jobs not yet completed or cancelled.",
            },
            {
                "key": "production_shops",
                "label": "Production shops",
                "value": shops.count(),
                "comparison_key": "month",
                "metric_key": "users",
                "helper_text": "Shops registered in the marketplace.",
            },
            {
                "key": "pending_callbacks",
                "label": "Pending Safaricom callbacks",
                "value": job_payments.filter(pending_callback_filter).count() + billing_transactions.filter(billing_pending_callback_filter).count(),
                "comparison_key": "hour",
                "metric_key": "confirmed_payments",
                "helper_text": "Initiated/pending payments waiting on callback confirmation.",
            },
            {
                "key": "samples_paid",
                "label": "Samples paid",
                "value": None,
                "comparison_key": "day",
                "metric_key": "samples_paid",
                "helper_text": "Unavailable: no dedicated sample flag is modeled on jobs or payments.",
            },
            {
                "key": "platform_margin",
                "label": "Platform/service margin",
                "value": _serialize_number(money_sum(managed_jobs, "platform_fee")),
                "comparison_key": "week",
                "metric_key": "payment_amount_collected",
                "helper_text": "Summed from managed-job platform fee fields.",
            },
        ],
        "metrics": {
            "users": _comparison_payload("New signups", users, "date_joined"),
            "quote_requests": _comparison_payload("Quote requests", quote_requests, "created_at"),
            "jobs_created": _comparison_payload("Jobs created", managed_jobs, "created_at"),
            "completed_jobs": _comparison_payload(
                "Completed jobs",
                managed_jobs.exclude(completed_at__isnull=True),
                "completed_at",
            ),
            "confirmed_payments": _comparison_payload(
                "Confirmed payments",
                confirmed_job_payments,
                "confirmed_at",
            ),
            "payment_amount_collected": _comparison_payload(
                "Payment amount collected",
                confirmed_job_payments,
                "confirmed_at",
                amount_field="received_amount",
                fallback_amount_field="amount",
            ),
            "calculator_previews": _unavailable_metric(
                "Calculator previews",
                "Unavailable: AnalyticsEvent does not currently store a dedicated calculator preview event type.",
            ),
            "samples_paid": _unavailable_metric(
                "Samples paid",
                "Unavailable: no explicit sample payment marker is modeled.",
            ),
        },
        "summaries": {
            "users": {
                "total_users": users.count(),
                "new_users_this_hour": _count_in_window(users, "date_joined", now.replace(minute=0, second=0, microsecond=0), now),
                "new_users_today": _count_in_window(users, "date_joined", now.replace(hour=0, minute=0, second=0, microsecond=0), now),
                "new_users_this_week": _count_in_window(users, "date_joined", now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday()), now),
                "new_users_this_month": _count_in_window(users, "date_joined", now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now),
                "clients": role_counts["client"],
                "partners": role_counts["partner"],
                "production_users": role_counts["production"],
                "super_admins": role_counts[CANONICAL_SUPER_ADMIN_ROLE],
                "active_users": users.exclude(last_login__isnull=True).count(),
                "verified_production_shops": None,
                "verified_production_shops_unavailable_reason": "Unavailable: Shop has no verification field.",
            },
            "quotes": {
                "total_quote_requests": quote_requests.count(),
                "pending_quote_requests": status_count(quote_requests, "status", QuoteRequest.DRAFT, QuoteRequest.SUBMITTED, QuoteRequest.VIEWED),
                "accepted_quote_requests": status_count(quote_requests, "status", QuoteRequest.ACCEPTED),
                "rejected_or_lost": status_count(shop_quotes, "status", ShopQuote.REJECTED, ShopQuote.DECLINED, ShopQuote.EXPIRED),
                "converted_to_jobs": managed_jobs.exclude(source_quote_request__isnull=True).count(),
            },
            "jobs": {
                "total_jobs": managed_jobs.count(),
                "jobs_pending": status_count(managed_jobs, "status", ManagedJobStatus.DRAFT, ManagedJobStatus.QUOTED, ManagedJobStatus.AWAITING_PAYMENT),
                "jobs_in_production": status_count(managed_jobs, "status", ManagedJobStatus.ASSIGNED, ManagedJobStatus.IN_PRODUCTION, ManagedJobStatus.FINISHING),
                "jobs_completed": status_count(managed_jobs, "status", ManagedJobStatus.COMPLETED),
                "jobs_cancelled": status_count(managed_jobs, "status", ManagedJobStatus.CANCELLED),
                "jobs_ready_for_pickup_or_delivery": status_count(managed_jobs, "status", ManagedJobStatus.READY, ManagedJobStatus.DELIVERED),
                "overdue_jobs": assignments.filter(due_at__lt=now).exclude(status__in=[JobAssignmentStatus.COMPLETED, JobAssignmentStatus.CANCELLED]).count(),
            },
            "payments": {
                "total_payments_initiated": job_payments.count() + billing_transactions.count(),
                "payments_confirmed": confirmed_job_payments.count() + confirmed_billing_payments.count(),
                "payments_pending_callback": job_payments.filter(pending_callback_filter).count() + billing_transactions.filter(billing_pending_callback_filter).count(),
                "payments_failed": job_payments.filter(payment_status=JobPaymentStatus.FAILED).count() + billing_transactions.filter(status=PaymentStatus.FAILED).count(),
                "payments_cancelled_or_timeout": job_payments.filter(payment_status=JobPaymentStatus.CANCELLED).count() + billing_transactions.filter(status=PaymentStatus.CANCELLED).count(),
                "mpesa_amount_collected": _serialize_number(total_collected),
                "sample_payments_collected": None,
                "sample_payments_unavailable_reason": "Unavailable: no sample payment marker is modeled.",
                "full_job_payments_collected": _serialize_number(money_sum(confirmed_job_payments, "received_amount", fallback_field="amount")),
                "pending_production_payouts": production_payouts_pending.count(),
                "pending_broker_margin_payouts": broker_payouts_pending.count(),
                "failed_callback_count": job_payments.filter(reconciliation_status__in=["failed", "manual_review", "amount_mismatch", "unknown_reference"]).count(),
            },
            "revenue": {
                "gross_client_revenue": _serialize_number(money_sum(managed_jobs, "client_total")),
                "production_base_cost": _serialize_number(money_sum(managed_jobs, "production_total")),
                "broker_margin": _serialize_number(money_sum(managed_jobs, "broker_commission")),
                "platform_service_fee": _serialize_number(money_sum(managed_jobs, "platform_fee")),
                "estimated_profit_service_fee": _serialize_number(money_sum(managed_jobs, "platform_fee")),
                "unpaid_revenue": _serialize_number(
                    money_sum(
                        managed_jobs.filter(payment_status__in=[ManagedJobPaymentStatus.PENDING, ManagedJobPaymentStatus.CONFIRMATION_PENDING]),
                        "client_total",
                    )
                ),
                "pending_amount": _serialize_number(
                    money_sum(
                        job_payments.filter(payment_status__in=[JobPaymentStatus.INITIATED, JobPaymentStatus.PENDING]),
                        "amount",
                    )
                ),
            },
            "funnel": [
                {"key": "calculator_preview", "label": "Calculator preview", "value": None, "unavailable_reason": "No dedicated calculator preview event type is tracked."},
                {"key": "quote_draft", "label": "Quote draft", "value": drafts.count()},
                {"key": "quote_requested", "label": "Quote requested", "value": quote_requests.count()},
                {"key": "quote_accepted", "label": "Quote accepted", "value": shop_quotes.filter(status=ShopQuote.ACCEPTED).count()},
                {"key": "sample_paid", "label": "Sample paid", "value": None, "unavailable_reason": "No explicit sample flag is modeled."},
                {"key": "full_payment", "label": "Full payment", "value": confirmed_job_payments.count()},
                {"key": "sent_to_production", "label": "Sent to production", "value": status_count(managed_jobs, "status", ManagedJobStatus.ASSIGNED, ManagedJobStatus.IN_PRODUCTION, ManagedJobStatus.FINISHING, ManagedJobStatus.READY, ManagedJobStatus.COMPLETED)},
                {"key": "completed", "label": "Completed", "value": managed_jobs.filter(status=ManagedJobStatus.COMPLETED).count()},
            ],
            "production": {
                "total_production_shops": shops.count(),
                "verified_shops": None,
                "verified_shops_unavailable_reason": "Unavailable: Shop has no verification flag.",
                "shops_with_active_pricing": shops.filter(pricing_ready=True).count(),
                "shops_missing_pricing": shops.filter(pricing_ready=False).count(),
                "shops_with_pending_jobs": shops.filter(managed_jobs__status__in=[ManagedJobStatus.ASSIGNED, ManagedJobStatus.IN_PRODUCTION, ManagedJobStatus.FINISHING]).distinct().count(),
                "shops_with_delayed_jobs": shops.filter(job_assignments__due_at__lt=now).exclude(job_assignments__status__in=[JobAssignmentStatus.COMPLETED, JobAssignmentStatus.CANCELLED]).distinct().count(),
                "low_stock_warnings": None,
                "low_stock_warnings_unavailable_reason": "Unavailable: no stock model exists in the current backend.",
            },
            "partners": {
                "active_partners": role_counts["partner"],
                "quotes_handled": managed_jobs.exclude(broker__isnull=True).count(),
                "broker_margin_earned": _serialize_number(money_sum(managed_jobs, "broker_commission")),
                "pending_broker_payouts": broker_payouts_pending.count(),
                "sample_conversions": None,
                "sample_conversions_unavailable_reason": "Unavailable: no explicit sample conversion tracking exists.",
            },
            "clients": {
                "new_clients_this_month": users.filter(date_joined__gte=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).count(),
                "returning_clients": users.filter(managed_jobs__isnull=False).annotate(job_count=Count("managed_jobs")).filter(job_count__gt=1).count(),
                "active_quote_drafts": drafts.filter(status=QuoteDraft.Status.DRAFT).count(),
                "repeat_orders": managed_jobs.filter(client__isnull=False).values("client_id").annotate(job_count=Count("id")).filter(job_count__gt=1).count(),
                "abandoned_quote_drafts": drafts.filter(status=QuoteDraft.Status.DRAFT, updated_at__lt=now - timedelta(days=7)).count(),
            },
            "activity": {
                "calculator_previews": None,
                "calculator_previews_unavailable_reason": "Unavailable: calculator preview analytics are not tracked as a dedicated event.",
                "artwork_uploads": None,
                "artwork_uploads_unavailable_reason": "Unavailable: artwork uploads are not tracked as a dedicated analytics event.",
                "quote_drafts_created": drafts.count(),
                "top_product_types": [],
                "top_finished_sizes": [],
                "top_paper_stocks": [],
                "top_cities": list(
                    analytics_events.exclude(city="").values("city").annotate(count=Count("id")).order_by("-count", "city")[:5]
                ),
            },
        },
        "payments_monitor": {
            "statuses": {
                "initiated": job_payments.filter(payment_status=JobPaymentStatus.INITIATED).count() + billing_transactions.filter(status=PaymentStatus.INITIATED).count(),
                "pending_callback": job_payments.filter(pending_callback_filter).count() + billing_transactions.filter(billing_pending_callback_filter).count(),
                "confirmed": confirmed_job_payments.count() + confirmed_billing_payments.count(),
                "failed": job_payments.filter(payment_status=JobPaymentStatus.FAILED).count() + billing_transactions.filter(status=PaymentStatus.FAILED).count(),
                "cancelled": job_payments.filter(payment_status=JobPaymentStatus.CANCELLED).count() + billing_transactions.filter(status=PaymentStatus.CANCELLED).count(),
                "timeout": billing_transactions.filter(result_desc__icontains="timeout").count(),
                "reversed": None,
                "reversed_unavailable_reason": "Unavailable: reversal handling is not modeled.",
            },
            "latest_transactions": latest_payments,
        },
        "tables": {
            "latest_quotes": latest_quotes,
            "latest_jobs": latest_jobs,
            "latest_payments": latest_payments,
            "shops_needing_attention": shops_needing_attention,
            "recent_users": recent_users,
        },
    }
