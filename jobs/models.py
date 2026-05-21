"""
JobShare models: JobRequest (overflow work to share), JobClaim (printer claiming), JobNotification.
"""
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel

from .choices import (
    JobClaimStatus,
    JobAssignmentStatus,
    JobFileStatus,
    JobFileType,
    JobFileVisibility,
    JobMachineType,
    JobPaymentMethod,
    JobPaymentChannel,
    JobPaymentReconciliationStatus,
    JobPaymentStatus,
    JobSettlementStatus,
    JobRequestStatus,
    ManagedJobAssignmentStatus,
    ManagedJobExceptionStatus,
    ManagedJobFulfillmentMode,
    ManagedJobPaymentStatus,
    ManagedJobStatus,
    ManagedJobUrgencyType,
    ManagedJobTopologyType,
)


def _generate_public_token():
    """Generate un-guessable token (32 bytes = 43 chars base64url)."""
    return secrets.token_urlsafe(32)


class JobRequest(TimeStampedModel):
    """Overflow job a printer wants to share with others."""

    OPEN = JobRequestStatus.OPEN
    CLAIMED = JobRequestStatus.CLAIMED
    CLOSED = JobRequestStatus.CLOSED
    STATUS_CHOICES = JobRequestStatus.choices

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_requests",
        verbose_name=_("created by"),
    )
    title = models.CharField(
        max_length=255,
        verbose_name=_("title"),
        help_text=_("Short title for the job."),
    )
    specs = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("specs"),
        help_text=_("Job specifications (product, quantity, paper, etc.)."),
    )
    location = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("location"),
        help_text=_("Location or area for the job."),
    )
    deadline = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("deadline"),
        help_text=_("When the job needs to be done."),
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=OPEN,
        verbose_name=_("status"),
    )
    public_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("public token"),
        help_text=_("Un-guessable token for public share URL."),
    )
    machine_type = models.CharField(
        max_length=30,
        choices=JobMachineType.choices,
        default=JobMachineType.DIGITAL,
        verbose_name=_("machine type"),
        help_text=_("Required machine type (DIGITAL, LARGE_FORMAT, UV, etc)."),
    )
    finishing_capabilities = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("finishing capabilities needed"),
        help_text=_("List of finishing capabilities required (e.g. ['lamination', 'cutting'])."),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("job request")
        verbose_name_plural = _("job requests")

    def __str__(self):
        return f"{self.title} ({self.status})"

    def ensure_public_token(self):
        """Generate public_token if not set."""
        if not self.public_token:
            self.public_token = _generate_public_token()
            self.save(update_fields=["public_token", "updated_at"])
        return self.public_token


class JobClaim(TimeStampedModel):
    """A printer's claim on a job request."""

    PENDING = JobClaimStatus.PENDING
    ACCEPTED = JobClaimStatus.ACCEPTED
    REJECTED = JobClaimStatus.REJECTED
    STATUS_CHOICES = JobClaimStatus.choices

    job_request = models.ForeignKey(
        JobRequest,
        on_delete=models.CASCADE,
        related_name="claims",
        verbose_name=_("job request"),
    )
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_claims",
        verbose_name=_("claimed by"),
    )
    price_offered = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("price offered"),
        help_text=_("Optional price the claimant offers."),
    )
    message = models.TextField(
        blank=True,
        default="",
        verbose_name=_("message"),
        help_text=_("Message from the claimant."),
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING,
        verbose_name=_("status"),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("job claim")
        verbose_name_plural = _("job claims")
        constraints = [
            models.UniqueConstraint(
                fields=["job_request", "claimed_by"],
                name="unique_job_claim",
            ),
        ]

    def __str__(self):
        return f"{self.job_request.title} — {self.claimed_by.email} ({self.status})"


class JobNotification(TimeStampedModel):
    """Notification for claimant when their claim is accepted."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_notifications",
        verbose_name=_("user"),
    )
    job_request = models.ForeignKey(
        JobRequest,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name=_("job request"),
    )
    job_claim = models.ForeignKey(
        JobClaim,
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
        verbose_name=_("job claim"),
    )
    message = models.TextField(
        default="",
        verbose_name=_("message"),
        help_text=_("Notification message."),
    )
    read_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("read at"),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("job notification")
        verbose_name_plural = _("job notifications")

    def __str__(self):
        return f"JobNotification #{self.id} for {self.user.email}"


class ManagedJob(TimeStampedModel):
    """Platform-owned workflow anchor for managed operational orchestration."""

    managed_reference = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        default="",
        verbose_name=_("managed reference"),
        help_text=_("Stable reference for the managed operational job."),
    )
    title = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("title"),
        help_text=_("Operational label for the managed job."),
    )
    tracking_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        verbose_name=_("tracking token"),
        help_text=_("Public tracking token for managed job status links."),
    )
    source_quote_request = models.ForeignKey(
        "quotes.QuoteRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_jobs",
        verbose_name=_("source quote request"),
    )
    source_shop_quote = models.ForeignKey(
        "quotes.ShopQuote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_jobs",
        verbose_name=_("source shop quote"),
    )
    source_production_order = models.ForeignKey(
        "production.ProductionOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_jobs",
        verbose_name=_("source production order"),
    )
    source_job_request = models.ForeignKey(
        "jobs.JobRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_derivatives",
        verbose_name=_("source overflow job request"),
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_jobs",
        verbose_name=_("client"),
    )
    customer = models.ForeignKey(
        "production.Customer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_jobs",
        verbose_name=_("customer"),
    )
    broker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="broker_managed_jobs",
        verbose_name=_("broker"),
    )
    assigned_shop = models.ForeignKey(
        "shops.Shop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_jobs",
        verbose_name=_("assigned shop"),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_jobs_created",
        verbose_name=_("created by"),
    )
    status = models.CharField(
        max_length=32,
        choices=ManagedJobStatus.choices,
        default=ManagedJobStatus.DRAFT,
        verbose_name=_("status"),
    )
    payment_status = models.CharField(
        max_length=32,
        choices=ManagedJobPaymentStatus.choices,
        default=ManagedJobPaymentStatus.PENDING,
        verbose_name=_("payment status"),
    )
    assignment_status = models.CharField(
        max_length=32,
        choices=ManagedJobAssignmentStatus.choices,
        default=ManagedJobAssignmentStatus.UNASSIGNED,
        verbose_name=_("assignment status"),
    )
    exception_status = models.CharField(
        max_length=32,
        choices=ManagedJobExceptionStatus.choices,
        default=ManagedJobExceptionStatus.CLEAR,
        verbose_name=_("exception status"),
    )
    fulfillment_mode = models.CharField(
        max_length=32,
        choices=ManagedJobFulfillmentMode.choices,
        default=ManagedJobFulfillmentMode.PICKUP,
        verbose_name=_("fulfillment mode"),
    )
    topology_type = models.CharField(
        max_length=32,
        choices=ManagedJobTopologyType.choices,
        default=ManagedJobTopologyType.CLIENT_PRINTY_SUPPORT,
        verbose_name=_("topology type"),
    )
    urgency_type = models.CharField(
        max_length=32,
        choices=ManagedJobUrgencyType.choices,
        default=ManagedJobUrgencyType.STANDARD,
        verbose_name=_("urgency type"),
    )
    urgency_multiplier = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    urgency_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    after_hours_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    requested_deadline = models.DateTimeField(null=True, blank=True)
    requested_delivery_time = models.DateTimeField(null=True, blank=True)
    operational_priority_level = models.PositiveSmallIntegerField(default=1)
    client_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    production_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    platform_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    broker_commission = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    payout_hold = models.BooleanField(default=False)
    dispute_open = models.BooleanField(default=False)
    production_issue_flag = models.BooleanField(default=False)
    delivery_issue_flag = models.BooleanField(default=False)
    ops_review_required = models.BooleanField(default=False)
    commercial_snapshot = models.JSONField(default=dict, blank=True)
    operational_snapshot = models.JSONField(default=dict, blank=True)
    workflow_metadata = models.JSONField(default=dict, blank=True)
    relationship_snapshot = models.JSONField(default=dict, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_jobs_dispatched",
        verbose_name=_("dispatched by"),
    )
    assigned_at = models.DateTimeField(null=True, blank=True)
    production_started_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    disputed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("managed job")
        verbose_name_plural = _("managed jobs")
        indexes = [
            models.Index(fields=["status", "payment_status"], name="managed_job_status_payment_idx"),
            models.Index(fields=["assigned_shop", "assignment_status"], name="managed_job_assignment_idx"),
            models.Index(fields=["operational_priority_level", "status"], name="managed_job_priority_idx"),
        ]

    def __str__(self):
        return self.managed_reference or self.title or f"ManagedJob #{self.id}"

    def save(self, *args, **kwargs):
        if not self.managed_reference:
            self.managed_reference = f"MJ-{self.pk or 'new'}"
        super().save(*args, **kwargs)
        if self.managed_reference.endswith("-new"):
            self.managed_reference = f"MJ-{self.id}"
            super().save(update_fields=["managed_reference", "updated_at"])


class JobAssignment(TimeStampedModel):
    """Shop production responsibility layer beneath ManagedJob."""

    managed_job = models.ForeignKey(
        ManagedJob,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("managed job"),
    )
    assigned_shop = models.ForeignKey(
        "shops.Shop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_assignments",
        verbose_name=_("assigned shop"),
    )
    source_shop_quote = models.ForeignKey(
        "quotes.ShopQuote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_assignments",
        verbose_name=_("source shop quote"),
    )
    production_order = models.ForeignKey(
        "production.ProductionOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_assignments",
        verbose_name=_("production order"),
    )
    status = models.CharField(
        max_length=32,
        choices=JobAssignmentStatus.choices,
        default=JobAssignmentStatus.PENDING,
        verbose_name=_("status"),
    )
    production_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    urgency_type = models.CharField(
        max_length=32,
        choices=ManagedJobUrgencyType.choices,
        default=ManagedJobUrgencyType.STANDARD,
        verbose_name=_("urgency type"),
    )
    operational_priority_level = models.PositiveSmallIntegerField(default=1)
    due_at = models.DateTimeField(null=True, blank=True)
    requested_deadline = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    reassigned_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reassignments",
        verbose_name=_("reassigned from"),
    )
    assignment_notes = models.TextField(blank=True, default="")
    operational_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("job assignment")
        verbose_name_plural = _("job assignments")
        constraints = [
            models.UniqueConstraint(
                fields=["managed_job"],
                condition=models.Q(reassigned_from__isnull=True),
                name="unique_active_assignment_per_managed_job",
            ),
        ]
        indexes = [
            models.Index(fields=["assigned_shop", "status"], name="job_assignment_shop_status_idx"),
            models.Index(fields=["assigned_shop", "operational_priority_level"], name="job_assignment_priority_idx"),
        ]

    def __str__(self):
        return f"Assignment #{self.id} for {self.managed_job.managed_reference or self.managed_job_id}"


class JobFile(TimeStampedModel):
    """Canonical managed-job file ownership record."""

    managed_job = models.ForeignKey(
        ManagedJob,
        on_delete=models.CASCADE,
        related_name="job_files",
        verbose_name=_("managed job"),
    )
    assignment = models.ForeignKey(
        JobAssignment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_files",
        verbose_name=_("assignment"),
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_job_files",
        verbose_name=_("uploaded by"),
    )
    file = models.FileField(
        upload_to="managed_jobs/%Y/%m/",
        null=True,
        blank=True,
        verbose_name=_("file"),
    )
    original_filename = models.CharField(max_length=255, blank=True, default="")
    file_type = models.CharField(
        max_length=32,
        choices=JobFileType.choices,
        default=JobFileType.CUSTOMER_UPLOAD,
        verbose_name=_("file type"),
    )
    visibility = models.CharField(
        max_length=16,
        choices=JobFileVisibility.choices,
        default=JobFileVisibility.CLIENT,
        verbose_name=_("visibility"),
    )
    status = models.CharField(
        max_length=32,
        choices=JobFileStatus.choices,
        default=JobFileStatus.UPLOADED,
        verbose_name=_("status"),
    )
    version = models.PositiveIntegerField(default=1)
    notes = models.TextField(blank=True, default="")
    replaces = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revisions",
        verbose_name=_("replaces"),
    )
    source_uploaded_artwork = models.ForeignKey(
        "artwork.UploadedArtwork",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_files",
        verbose_name=_("source uploaded artwork"),
    )
    source_quote_request_attachment = models.ForeignKey(
        "quotes.QuoteRequestAttachment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_files",
        verbose_name=_("source quote request attachment"),
    )
    source_shop_quote_attachment = models.ForeignKey(
        "quotes.ShopQuoteAttachment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_files",
        verbose_name=_("source shop quote attachment"),
    )

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = _("job file")
        verbose_name_plural = _("job files")
        constraints = [
            models.UniqueConstraint(
                fields=["managed_job", "source_uploaded_artwork"],
                condition=models.Q(source_uploaded_artwork__isnull=False),
                name="unique_job_file_source_uploaded_artwork",
            ),
            models.UniqueConstraint(
                fields=["managed_job", "source_quote_request_attachment"],
                condition=models.Q(source_quote_request_attachment__isnull=False),
                name="unique_job_file_source_quote_attachment",
            ),
            models.UniqueConstraint(
                fields=["managed_job", "source_shop_quote_attachment"],
                condition=models.Q(source_shop_quote_attachment__isnull=False),
                name="unique_job_file_source_shop_attachment",
            ),
        ]
        indexes = [
            models.Index(fields=["managed_job", "file_type"], name="job_file_type_idx"),
            models.Index(fields=["managed_job", "visibility"], name="job_file_visibility_idx"),
        ]

    def __str__(self):
        return self.original_filename or f"JobFile #{self.id}"


class JobPayment(TimeStampedModel):
    """Job-level customer payment confirmation record."""

    managed_job = models.ForeignKey(
        ManagedJob,
        on_delete=models.CASCADE,
        related_name="payments",
        verbose_name=_("managed job"),
    )
    payer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_payments",
        verbose_name=_("payer"),
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(
        max_length=16,
        choices=JobPaymentMethod.choices,
        default=JobPaymentMethod.MPESA,
        verbose_name=_("payment method"),
    )
    payment_status = models.CharField(
        max_length=32,
        choices=JobPaymentStatus.choices,
        default=JobPaymentStatus.PENDING,
        verbose_name=_("payment status"),
    )
    payment_channel = models.CharField(
        max_length=32,
        choices=JobPaymentChannel.choices,
        default=JobPaymentChannel.STK_PUSH,
        verbose_name=_("payment channel"),
    )
    reconciliation_status = models.CharField(
        max_length=32,
        choices=JobPaymentReconciliationStatus.choices,
        default=JobPaymentReconciliationStatus.PENDING,
        verbose_name=_("reconciliation status"),
    )
    account_reference = models.CharField(max_length=100, blank=True, default="", db_index=True)
    checkout_request_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    merchant_request_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    mpesa_receipt_number = models.CharField(max_length=50, blank=True, default="", db_index=True)
    payer_phone = models.CharField(max_length=20, blank=True, default="")
    expected_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    received_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    external_reference = models.CharField(max_length=100, blank=True, default="")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    raw_gateway_payload = models.JSONField(null=True, blank=True)
    callback_payload = models.JSONField(null=True, blank=True)
    query_payload = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("job payment")
        verbose_name_plural = _("job payments")
        indexes = [
            models.Index(fields=["managed_job", "payment_status"], name="job_payment_status_idx"),
            models.Index(fields=["account_reference"], name="job_payment_account_ref_idx"),
            models.Index(fields=["checkout_request_id"], name="job_payment_checkout_idx"),
            models.Index(fields=["merchant_request_id"], name="job_payment_merchant_idx"),
            models.Index(fields=["mpesa_receipt_number"], name="job_payment_receipt_idx"),
        ]

    def __str__(self):
        return f"JobPayment #{self.id} for {self.managed_job.managed_reference or self.managed_job_id}"


class JobSettlementSplit(TimeStampedModel):
    """Internal settlement split for a managed job."""

    COMMISSION_OWNER_PRINTY = "printy"
    COMMISSION_OWNER_USER = "user"
    COMMISSION_OWNER_SHOP = "shop"
    COMMISSION_OWNER_CHOICES = [
        (COMMISSION_OWNER_PRINTY, _("Printy")),
        (COMMISSION_OWNER_USER, _("User")),
        (COMMISSION_OWNER_SHOP, _("Shop")),
    ]

    managed_job = models.OneToOneField(
        ManagedJob,
        on_delete=models.CASCADE,
        related_name="settlement_split",
        verbose_name=_("managed job"),
    )
    production_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    platform_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    partner_commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    client_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    relationship_owner_type = models.CharField(max_length=20, blank=True, default="")
    relationship_owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_settlement_splits",
        verbose_name=_("relationship owner user"),
    )
    relationship_owner_shop = models.ForeignKey(
        "shops.Shop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_settlement_splits",
        verbose_name=_("relationship owner shop"),
    )
    relationship_owner_reference = models.CharField(max_length=50, blank=True, default="")
    commission_recipient_type = models.CharField(
        max_length=20,
        choices=COMMISSION_OWNER_CHOICES,
        default=COMMISSION_OWNER_PRINTY,
        verbose_name=_("commission recipient type"),
    )
    status = models.CharField(
        max_length=20,
        choices=JobSettlementStatus.choices,
        default=JobSettlementStatus.PENDING,
        verbose_name=_("status"),
    )
    payment_method = models.CharField(
        max_length=16,
        choices=JobPaymentMethod.choices,
        default=JobPaymentMethod.MPESA,
        verbose_name=_("payment method"),
    )
    release_ready_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("job settlement split")
        verbose_name_plural = _("job settlement splits")

    def __str__(self):
        return f"Settlement for {self.managed_job.managed_reference or self.managed_job_id}"


class ManagedJobEvent(TimeStampedModel):
    """Lightweight audit trail for managed-job workflow activity."""

    managed_job = models.ForeignKey(
        ManagedJob,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("managed job"),
    )
    assignment = models.ForeignKey(
        JobAssignment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        verbose_name=_("assignment"),
    )
    job_file = models.ForeignKey(
        JobFile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        verbose_name=_("job file"),
    )
    payment = models.ForeignKey(
        JobPayment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        verbose_name=_("payment"),
    )
    settlement = models.ForeignKey(
        JobSettlementSplit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        verbose_name=_("settlement"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_job_events",
        verbose_name=_("actor"),
    )
    event_type = models.CharField(max_length=64, verbose_name=_("event type"))
    summary = models.CharField(max_length=255, blank=True, default="", verbose_name=_("summary"))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_("metadata"))

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("managed job event")
        verbose_name_plural = _("managed job events")
        indexes = [
            models.Index(fields=["managed_job", "event_type"], name="managed_job_event_type_idx"),
            models.Index(fields=["managed_job", "-created_at"], name="managed_job_event_created_idx"),
        ]

    def __str__(self):
        return f"{self.event_type} on {self.managed_job.managed_reference or self.managed_job_id}"
