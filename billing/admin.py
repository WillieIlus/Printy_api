"""Billing admin for plans, subscriptions, payments, renewals, and usage counters."""
from django.contrib import admin

from billing.models import (
    Plan,
    PaymentTransaction,
    RenewalAttempt,
    ShopSubscription,
    SubscriptionShop,
    UsageCounter,
)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "price_monthly", "price_annual", "shops_limit", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    list_filter = ["is_active", "analytics_level"]
    search_fields = ["name", "code"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["sort_order"]
    fieldsets = [
        ("Identity", {"fields": ["code", "name", "public_tagline", "best_for", "sort_order", "is_active"]}),
        ("Pricing", {"fields": ["price_monthly", "price_annual", "currency"]}),
        ("Limits", {"fields": ["shops_limit", "machines_limit", "products_limit", "quotes_per_month_limit", "users_limit"]}),
        ("Features", {"fields": ["all_papers_enabled", "branded_quotes_enabled", "customer_history_enabled", "analytics_level", "priority_support"]}),
        ("Content", {"fields": ["benefits", "metadata"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]


class SubscriptionShopInline(admin.TabularInline):
    model = SubscriptionShop
    extra = 0
    readonly_fields = ["added_at"]


@admin.register(ShopSubscription)
class ShopSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["owner_email", "plan", "billing_interval", "status", "ends_at", "over_limit", "auto_renew_enabled"]
    list_filter = ["status", "billing_interval", "plan", "over_limit", "auto_renew_enabled"]
    search_fields = ["owner__email", "payment_phone_e164", "mpesa_reference_last"]
    readonly_fields = ["created_at", "updated_at", "last_renewal_attempt_at", "next_renewal_attempt_at", "suspended_at", "cancelled_at"]
    inlines = [SubscriptionShopInline]
    actions = ["action_mark_active", "action_mark_suspended"]
    ordering = ["-created_at"]
    fieldsets = [
        ("Owner & Plan", {"fields": ["owner", "plan", "billing_interval"]}),
        ("Status & Dates", {"fields": ["status", "starts_at", "ends_at", "renews_at", "grace_period_ends_at", "auto_renew_enabled"]}),
        ("Payment", {"fields": ["payment_phone_e164", "mpesa_reference_last"]}),
        ("Flags", {"fields": ["over_limit"]}),
        ("Cancellation", {"fields": ["cancellation_requested_at", "cancelled_at", "suspended_at"], "classes": ["collapse"]}),
        ("Renewal", {"fields": ["last_renewal_attempt_at", "next_renewal_attempt_at"], "classes": ["collapse"]}),
        ("Notes", {"fields": ["notes"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"], "classes": ["collapse"]}),
    ]

    @admin.display(description="Owner email")
    def owner_email(self, obj):
        return obj.owner.email

    @admin.action(description="Mark selected subscriptions as Active")
    def action_mark_active(self, request, queryset):
        for sub in queryset:
            sub.mark_active()
        self.message_user(request, f"{queryset.count()} subscription(s) marked active.")

    @admin.action(description="Mark selected subscriptions as Suspended")
    def action_mark_suspended(self, request, queryset):
        for sub in queryset:
            sub.mark_suspended()
        self.message_user(request, f"{queryset.count()} subscription(s) suspended.")


@admin.register(SubscriptionShop)
class SubscriptionShopAdmin(admin.ModelAdmin):
    list_display = ["subscription", "shop", "is_primary", "added_at"]
    list_filter = ["is_primary"]
    search_fields = ["shop__name", "subscription__owner__email"]
    readonly_fields = ["added_at"]


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = [
        "owner_email",
        "amount",
        "currency",
        "status",
        "transaction_type",
        "phone_number",
        "mpesa_receipt_number",
        "checkout_request_id",
        "created_at",
    ]
    list_filter = ["status", "transaction_type", "provider_method", "provider"]
    search_fields = [
        "owner__email",
        "phone_number",
        "mpesa_receipt_number",
        "checkout_request_id",
        "merchant_request_id",
        "account_reference",
        "external_reference",
    ]
    readonly_fields = [
        "raw_request",
        "raw_response",
        "raw_callback",
        "idempotency_key",
        "merchant_request_id",
        "checkout_request_id",
        "mpesa_receipt_number",
        "initiated_at",
        "callback_received_at",
        "completed_at",
        "created_at",
        "updated_at",
    ]
    ordering = ["-created_at"]
    fieldsets = [
        ("Ownership", {"fields": ["owner", "subscription", "shop", "plan"]}),
        ("Transaction", {"fields": ["transaction_type", "provider", "provider_method", "amount", "currency", "phone_number", "account_reference", "transaction_desc", "external_reference"]}),
        ("Request response", {"fields": ["response_code", "response_description", "customer_message"]}),
        ("Callback result", {"fields": ["status", "result_code", "result_desc"]}),
        ("Daraja IDs", {"fields": ["merchant_request_id", "checkout_request_id", "mpesa_receipt_number"]}),
        ("Idempotency", {"fields": ["idempotency_key"]}),
        ("Timestamps", {"fields": ["initiated_at", "callback_received_at", "completed_at", "created_at", "updated_at"], "classes": ["collapse"]}),
        ("Raw payloads", {"fields": ["raw_request", "raw_response", "raw_callback"], "classes": ["collapse"]}),
    ]

    @admin.display(description="Owner email")
    def owner_email(self, obj):
        return obj.owner.email


@admin.register(RenewalAttempt)
class RenewalAttemptAdmin(admin.ModelAdmin):
    list_display = ["subscription", "attempt_number", "status", "due_at", "attempted_at", "next_retry_at"]
    list_filter = ["status"]
    search_fields = ["subscription__owner__email"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]
    actions = ["action_retry_renewal"]

    @admin.action(description="Re-queue selected renewal attempts")
    def action_retry_renewal(self, request, queryset):
        updated = queryset.filter(status__in=[RenewalAttempt.STATUS_FAILED, RenewalAttempt.STATUS_ABANDONED]).update(status=RenewalAttempt.STATUS_QUEUED)
        self.message_user(request, f"{updated} attempt(s) re-queued.")


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ["owner_email", "month", "quotes_created_count", "active_products_count_snapshot", "shops_count_snapshot"]
    list_filter = ["month"]
    search_fields = ["owner__email"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["-month"]

    @admin.display(description="Owner email")
    def owner_email(self, obj):
        return obj.owner.email
