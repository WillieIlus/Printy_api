"""Billing models for Printy.ke subscription system."""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from shops.models import Shop


class Plan(models.Model):
    """Subscription plan definition.  Exactly four tiers: Free, Biashara, Biashara Plus, Biashara Max."""

    ANALYTICS_BASIC = "basic"
    ANALYTICS_STANDARD = "standard"
    ANALYTICS_ADVANCED = "advanced"
    ANALYTICS_CHOICES = [
        (ANALYTICS_BASIC, "Basic"),
        (ANALYTICS_STANDARD, "Standard"),
        (ANALYTICS_ADVANCED, "Advanced"),
    ]

    CODE_FREE = "FREE"
    CODE_BIASHARA = "BIASHARA"
    CODE_BIASHARA_PLUS = "BIASHARA_PLUS"
    CODE_BIASHARA_MAX = "BIASHARA_MAX"
    CODE_CHOICES = [
        (CODE_FREE, "Free"),
        (CODE_BIASHARA, "Biashara"),
        (CODE_BIASHARA_PLUS, "Biashara Plus"),
        (CODE_BIASHARA_MAX, "Biashara Max"),
    ]

    code = models.CharField(
        max_length=30,
        unique=True,
        choices=CODE_CHOICES,
        verbose_name=_("code"),
        db_index=True,
    )
    name = models.CharField(max_length=60, verbose_name=_("name"))
    price_monthly = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("monthly price (KES)"),
    )
    price_annual = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("annual price (KES)"),
    )
    currency = models.CharField(max_length=3, default="KES", verbose_name=_("currency"))

    # Limits — null means unlimited
    shops_limit = models.PositiveSmallIntegerField(default=1, verbose_name=_("shops limit"))
    machines_limit = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name=_("machines limit"))
    products_limit = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name=_("products limit"))
    quotes_per_month_limit = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("quotes per month limit"))
    users_limit = models.PositiveSmallIntegerField(default=1, verbose_name=_("users limit"))

    # Feature flags
    all_papers_enabled = models.BooleanField(default=True, verbose_name=_("all papers enabled"))
    branded_quotes_enabled = models.BooleanField(default=False, verbose_name=_("branded quotes enabled"))
    customer_history_enabled = models.BooleanField(default=False, verbose_name=_("customer history enabled"))
    analytics_level = models.CharField(
        max_length=20,
        choices=ANALYTICS_CHOICES,
        default=ANALYTICS_BASIC,
        verbose_name=_("analytics level"),
    )
    priority_support = models.BooleanField(default=False, verbose_name=_("priority support"))

    # Display / metadata
    is_active = models.BooleanField(default=True, verbose_name=_("is active"))
    sort_order = models.PositiveSmallIntegerField(default=0, verbose_name=_("sort order"))
    public_tagline = models.CharField(max_length=120, blank=True, default="", verbose_name=_("public tagline"))
    best_for = models.CharField(max_length=200, blank=True, default="", verbose_name=_("best for"))
    benefits = models.JSONField(default=list, verbose_name=_("benefits"))
    metadata = models.JSONField(null=True, blank=True, verbose_name=_("metadata"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("plan")
        verbose_name_plural = _("plans")
        ordering = ["sort_order", "price_monthly"]

    def __str__(self):
        return self.name

    def get_price(self, interval: str) -> Decimal:
        """Return price for 'monthly' or 'annual' interval."""
        if interval == "annual":
            return self.price_annual
        return self.price_monthly

    @property
    def is_free(self) -> bool:
        return self.code == self.CODE_FREE

    def is_unlimited(self, field_name: str) -> bool:
        """Return True when the limit field is None (unlimited)."""
        return getattr(self, field_name) is None


class ShopSubscription(models.Model):
    """Owner-level subscription covering one or more shops."""

    INTERVAL_MONTHLY = "monthly"
    INTERVAL_ANNUAL = "annual"
    INTERVAL_CHOICES = [
        (INTERVAL_MONTHLY, "Monthly"),
        (INTERVAL_ANNUAL, "Annual"),
    ]

    STATUS_TRIALING = "trialing"
    STATUS_ACTIVE = "active"
    STATUS_PAST_DUE = "past_due"
    STATUS_GRACE = "grace_period"
    STATUS_SUSPENDED = "suspended"
    STATUS_CANCELLED = "cancelled"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_TRIALING, "Trialing"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_PAST_DUE, "Past due"),
        (STATUS_GRACE, "Grace period"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_EXPIRED, "Expired"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="billing_subscriptions",
        verbose_name=_("owner"),
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
        verbose_name=_("plan"),
    )
    billing_interval = models.CharField(
        max_length=10,
        choices=INTERVAL_CHOICES,
        default=INTERVAL_MONTHLY,
        verbose_name=_("billing interval"),
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        verbose_name=_("status"),
        db_index=True,
    )
    starts_at = models.DateTimeField(null=True, blank=True, verbose_name=_("starts at"))
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name=_("ends at"))
    renews_at = models.DateTimeField(null=True, blank=True, verbose_name=_("renews at"))
    auto_renew_enabled = models.BooleanField(default=True, verbose_name=_("auto renew enabled"))

    grace_period_ends_at = models.DateTimeField(null=True, blank=True, verbose_name=_("grace period ends at"))
    cancellation_requested_at = models.DateTimeField(null=True, blank=True, verbose_name=_("cancellation requested at"))
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name=_("cancelled at"))
    suspended_at = models.DateTimeField(null=True, blank=True, verbose_name=_("suspended at"))

    last_renewal_attempt_at = models.DateTimeField(null=True, blank=True, verbose_name=_("last renewal attempt at"))
    next_renewal_attempt_at = models.DateTimeField(null=True, blank=True, verbose_name=_("next renewal attempt at"))

    payment_phone_e164 = models.CharField(
        max_length=20, blank=True, default="", verbose_name=_("payment phone (E.164)")
    )
    mpesa_reference_last = models.CharField(
        max_length=100, blank=True, default="", verbose_name=_("last M-Pesa reference")
    )

    over_limit = models.BooleanField(default=False, verbose_name=_("over limit"))
    notes = models.TextField(blank=True, default="", verbose_name=_("notes"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("shop subscription")
        verbose_name_plural = _("shop subscriptions")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.owner.email} — {self.plan.name} ({self.get_status_display()})"

    def is_active_now(self) -> bool:
        """True when subscription grants access right now."""
        now = timezone.now()
        if self.status in (self.STATUS_ACTIVE, self.STATUS_TRIALING):
            if self.ends_at and now > self.ends_at:
                return False
            return True
        if self.status == self.STATUS_GRACE:
            return self.is_within_grace()
        return False

    def is_within_grace(self) -> bool:
        if not self.grace_period_ends_at:
            return False
        return timezone.now() <= self.grace_period_ends_at

    def remaining_days(self) -> int:
        if not self.ends_at:
            return 0
        return max(0, (self.ends_at - timezone.now()).days)

    def mark_past_due(self):
        self.status = self.STATUS_PAST_DUE
        self.save(update_fields=["status", "updated_at"])

    def mark_active(self):
        self.status = self.STATUS_ACTIVE
        self.grace_period_ends_at = None
        self.over_limit = False
        self.save(update_fields=["status", "grace_period_ends_at", "over_limit", "updated_at"])

    def mark_suspended(self):
        self.status = self.STATUS_SUSPENDED
        self.suspended_at = timezone.now()
        self.save(update_fields=["status", "suspended_at", "updated_at"])


class SubscriptionShop(models.Model):
    """Links a subscription to a shop (enforces per-plan shop count)."""

    subscription = models.ForeignKey(
        ShopSubscription,
        on_delete=models.CASCADE,
        related_name="subscription_shops",
        verbose_name=_("subscription"),
    )
    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="billing_subscription_shops",
        verbose_name=_("shop"),
    )
    is_primary = models.BooleanField(default=False, verbose_name=_("is primary"))
    added_at = models.DateTimeField(auto_now_add=True, verbose_name=_("added at"))

    class Meta:
        verbose_name = _("subscription shop")
        verbose_name_plural = _("subscription shops")
        unique_together = [("subscription", "shop")]

    def __str__(self):
        return f"{self.subscription} → {self.shop.name}"


class PaymentTransaction(models.Model):
    """One payment attempt — created before the external call, updated on callback."""

    TYPE_ACTIVATION = "activation"
    TYPE_RENEWAL = "renewal"
    TYPE_UPGRADE = "upgrade"
    TYPE_MANUAL_RETRY = "manual_retry"
    TYPE_CHOICES = [
        (TYPE_ACTIVATION, "Activation"),
        (TYPE_RENEWAL, "Renewal"),
        (TYPE_UPGRADE, "Upgrade"),
        (TYPE_MANUAL_RETRY, "Manual retry"),
    ]

    PROVIDER_MPESA = "mpesa"
    PROVIDER_CHOICES = [(PROVIDER_MPESA, "M-Pesa")]

    METHOD_STK = "stk_push"
    METHOD_RATIBA = "ratiba"
    METHOD_MANUAL = "manual_admin"
    METHOD_CHOICES = [
        (METHOD_STK, "STK Push"),
        (METHOD_RATIBA, "Ratiba"),
        (METHOD_MANUAL, "Manual (Admin)"),
    ]

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_TIMED_OUT = "timed_out"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_TIMED_OUT, "Timed out"),
    ]

    subscription = models.ForeignKey(
        ShopSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
        verbose_name=_("subscription"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="billing_transactions",
        verbose_name=_("owner"),
    )
    shop = models.ForeignKey(
        Shop,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_transactions",
        verbose_name=_("shop"),
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
        verbose_name=_("plan"),
    )
    transaction_type = models.CharField(
        max_length=20, choices=TYPE_CHOICES, verbose_name=_("transaction type")
    )
    provider = models.CharField(
        max_length=20, choices=PROVIDER_CHOICES, default=PROVIDER_MPESA, verbose_name=_("provider")
    )
    provider_method = models.CharField(
        max_length=20, choices=METHOD_CHOICES, default=METHOD_STK, verbose_name=_("provider method")
    )

    # Daraja STK push IDs
    merchant_request_id = models.CharField(
        max_length=100, blank=True, default="", db_index=True, verbose_name=_("merchant request ID")
    )
    checkout_request_id = models.CharField(
        max_length=100, blank=True, default="", db_index=True, verbose_name=_("checkout request ID")
    )
    mpesa_receipt_number = models.CharField(
        max_length=50, blank=True, default="", db_index=True, verbose_name=_("M-Pesa receipt number")
    )
    phone_number = models.CharField(max_length=20, blank=True, default="", verbose_name=_("phone number"))
    account_reference = models.CharField(max_length=100, blank=True, default="", verbose_name=_("account reference"))
    transaction_desc = models.CharField(max_length=255, blank=True, default="", verbose_name=_("transaction description"))

    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("amount"))
    currency = models.CharField(max_length=3, default="KES", verbose_name=_("currency"))

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING,
        verbose_name=_("status"), db_index=True,
    )
    result_code = models.CharField(max_length=10, blank=True, default="", verbose_name=_("result code"))
    result_desc = models.CharField(max_length=255, blank=True, default="", verbose_name=_("result description"))

    raw_request = models.JSONField(null=True, blank=True, verbose_name=_("raw STK request"))
    raw_callback = models.JSONField(null=True, blank=True, verbose_name=_("raw callback payload"))

    initiated_at = models.DateTimeField(null=True, blank=True, verbose_name=_("initiated at"))
    callback_received_at = models.DateTimeField(null=True, blank=True, verbose_name=_("callback received at"))
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("completed at"))

    idempotency_key = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        verbose_name=_("idempotency key"),
        help_text=_("Unique key to prevent duplicate processing."),
    )
    external_reference = models.CharField(max_length=100, blank=True, default="", verbose_name=_("external reference"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("payment transaction")
        verbose_name_plural = _("payment transactions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["checkout_request_id"], name="billing_txn_checkout_idx"),
            models.Index(fields=["merchant_request_id"], name="billing_txn_merchant_idx"),
            models.Index(fields=["mpesa_receipt_number"], name="billing_txn_receipt_idx"),
        ]

    def __str__(self):
        return f"{self.owner.email} — {self.amount} KES ({self.get_status_display()})"


class RenewalAttempt(models.Model):
    """Tracks each renewal attempt in the retry schedule."""

    STATUS_QUEUED = "queued"
    STATUS_INITIATED = "initiated"
    STATUS_AWAITING = "awaiting_callback"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_ABANDONED = "abandoned"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_INITIATED, "Initiated"),
        (STATUS_AWAITING, "Awaiting callback"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_ABANDONED, "Abandoned"),
    ]

    subscription = models.ForeignKey(
        ShopSubscription,
        on_delete=models.CASCADE,
        related_name="renewal_attempts",
        verbose_name=_("subscription"),
    )
    due_at = models.DateTimeField(verbose_name=_("due at"))
    attempted_at = models.DateTimeField(null=True, blank=True, verbose_name=_("attempted at"))
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED, verbose_name=_("status")
    )
    attempt_number = models.PositiveSmallIntegerField(default=1, verbose_name=_("attempt number"))
    payment_transaction = models.ForeignKey(
        PaymentTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="renewal_attempts",
        verbose_name=_("payment transaction"),
    )
    failure_reason = models.CharField(max_length=255, blank=True, default="", verbose_name=_("failure reason"))
    next_retry_at = models.DateTimeField(null=True, blank=True, verbose_name=_("next retry at"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("renewal attempt")
        verbose_name_plural = _("renewal attempts")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subscription} attempt #{self.attempt_number} ({self.get_status_display()})"


class UsageCounter(models.Model):
    """Monthly usage snapshot per owner — one row per owner per month."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="usage_counters",
        verbose_name=_("owner"),
    )
    subscription = models.ForeignKey(
        ShopSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usage_counters",
        verbose_name=_("subscription"),
    )
    month = models.DateField(
        verbose_name=_("month"),
        help_text=_("First day of the month this counter covers."),
    )

    quotes_created_count = models.PositiveIntegerField(default=0, verbose_name=_("quotes created"))
    active_products_count_snapshot = models.PositiveIntegerField(default=0, verbose_name=_("active products snapshot"))
    active_users_count_snapshot = models.PositiveIntegerField(default=0, verbose_name=_("active users snapshot"))
    active_machines_count_snapshot = models.PositiveIntegerField(default=0, verbose_name=_("active machines snapshot"))
    shops_count_snapshot = models.PositiveIntegerField(default=0, verbose_name=_("shops snapshot"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("usage counter")
        verbose_name_plural = _("usage counters")
        unique_together = [("owner", "month")]
        ordering = ["-month"]

    def __str__(self):
        return f"{self.owner.email} — {self.month:%Y-%m}"
