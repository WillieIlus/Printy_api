"""JobShare API serializers."""
from decimal import Decimal

from django.urls import reverse
from rest_framework import serializers

from api.visibility import (
    CLIENT_ACTOR,
    OPS_ACTOR,
    PARTNER_ACTOR,
    SHOP_ACTOR,
    can_actor_view_shop_name,
    resolve_actor,
)
from jobs.models import JobAssignment, JobClaim, JobFile, JobPayment, JobRequest, JobSettlementSplit, ManagedJob, ManagedJobEvent
from jobs.workflow import project_workflow_state


def _money(value) -> str | None:
    if value is None:
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except Exception:
        return None


class JobRequestCreateSerializer(serializers.ModelSerializer):
    """Create a job request (authenticated printer/staff)."""

    class Meta:
        model = JobRequest
        fields = ["title", "specs", "location", "deadline", "machine_type", "finishing_capabilities"]


class JobRequestListSerializer(serializers.ModelSerializer):
    """List job requests (safe fields)."""

    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    claims_count = serializers.SerializerMethodField()
    claims = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = JobRequest
        fields = [
            "id",
            "title",
            "specs",
            "location",
            "deadline",
            "status",
            "status_label",
            "created_by",
            "created_by_email",
            "created_at",
            "claims_count",
            "claims",
        ]

    def get_claims_count(self, obj):
        return obj.claims.count()

    def get_claims(self, obj):
        request = self.context.get("request")
        if request and request.user and obj.created_by_id == request.user.id:
            return JobClaimSerializer(obj.claims.all(), many=True).data
        return []


class JobRequestDetailSerializer(serializers.ModelSerializer):
    """Full detail for owner or authenticated users."""

    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    claims_count = serializers.SerializerMethodField()
    claims = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = JobRequest
        fields = [
            "id",
            "title",
            "specs",
            "location",
            "deadline",
            "status",
            "status_label",
            "machine_type",
            "finishing_capabilities",
            "created_by",
            "created_by_email",
            "created_at",
            "updated_at",
            "claims_count",
            "claims",
        ]

    def get_claims_count(self, obj):
        return obj.claims.count()

    def get_claims(self, obj):
        return JobClaimSerializer(obj.claims.all(), many=True).data


class JobClaimCreateSerializer(serializers.ModelSerializer):
    """Create a claim on a job request."""

    class Meta:
        model = JobClaim
        fields = ["price_offered", "message"]


class JobClaimSerializer(serializers.ModelSerializer):
    """Read claim with claimant info."""

    claimed_by_email = serializers.EmailField(source="claimed_by.email", read_only=True)
    job_request_title = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = JobClaim
        fields = [
            "id",
            "job_request",
            "job_request_title",
            "claimed_by",
            "claimed_by_email",
            "price_offered",
            "message",
            "status",
            "status_label",
            "created_at",
        ]

    def get_job_request_title(self, obj):
        return obj.job_request.title if obj.job_request_id else None


class JobRequestPublicSerializer(serializers.ModelSerializer):
    """Minimal safe fields for public token view. No internal data."""

    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = JobRequest
        fields = [
            "id",
            "title",
            "specs",
            "location",
            "deadline",
            "status",
            "status_label",
            "machine_type",
            "finishing_capabilities",
        ]


class ManagedJobSerializer(serializers.ModelSerializer):
    workflow_projection = serializers.SerializerMethodField()
    file_count = serializers.SerializerMethodField()
    payment_count = serializers.SerializerMethodField()
    urgency_label = serializers.SerializerMethodField()

    class Meta:
        model = ManagedJob
        fields = [
            "id",
            "managed_reference",
            "title",
            "status",
            "payment_status",
            "assignment_status",
            "exception_status",
            "fulfillment_mode",
            "topology_type",
            "payout_hold",
            "dispute_open",
            "production_issue_flag",
            "delivery_issue_flag",
            "ops_review_required",
            "urgency_type",
            "urgency_label",
            "urgency_fee",
            "after_hours_fee",
            "requested_deadline",
            "requested_delivery_time",
            "operational_priority_level",
            "file_count",
            "payment_count",
            "workflow_projection",
            "accepted_at",
            "payment_confirmed_at",
            "assigned_at",
            "ready_at",
            "delivered_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]

    def get_workflow_projection(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        return project_workflow_state(
            status=obj.status,
            actor=actor,
            payment_status=obj.payment_status,
            assignment_status=obj.assignment_status,
            exception_status=obj.exception_status,
            urgency_type=obj.urgency_type,
            operational_priority_level=obj.operational_priority_level,
        )

    def get_file_count(self, obj):
        return obj.job_files.count()

    def get_payment_count(self, obj):
        return obj.payments.count()

    def get_urgency_label(self, obj):
        return getattr(obj, "get_urgency_type_display", lambda: "")() or ""


class JobAssignmentSerializer(serializers.ModelSerializer):
    shop_name = serializers.SerializerMethodField()
    managed_reference = serializers.CharField(source="managed_job.managed_reference", read_only=True)
    managed_job_status = serializers.CharField(source="managed_job.status", read_only=True)
    managed_job_payment_status = serializers.CharField(source="managed_job.payment_status", read_only=True)
    workflow_projection = serializers.SerializerMethodField()
    urgency_label = serializers.CharField(source="get_urgency_type_display", read_only=True)

    class Meta:
        model = JobAssignment
        fields = [
            "id",
            "managed_job",
            "managed_reference",
            "assigned_shop",
            "shop_name",
            "status",
            "urgency_type",
            "urgency_label",
            "operational_priority_level",
            "managed_job_status",
            "managed_job_payment_status",
            "workflow_projection",
            "production_order",
            "due_at",
            "requested_deadline",
            "accepted_at",
            "rejected_at",
            "assignment_notes",
        ]

    def get_shop_name(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor in {SHOP_ACTOR, OPS_ACTOR, PARTNER_ACTOR} and can_actor_view_shop_name(actor=actor, topology_mode="managed"):
            return getattr(obj.assigned_shop, "name", "") if obj.assigned_shop_id else ""
        return "Verified Print Partner"

    def get_workflow_projection(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        return project_workflow_state(
            status=obj.managed_job.status,
            actor=actor,
            payment_status=obj.managed_job.payment_status,
            assignment_status=obj.managed_job.assignment_status,
            exception_status=obj.managed_job.exception_status,
            urgency_type=obj.urgency_type or obj.managed_job.urgency_type,
            operational_priority_level=obj.operational_priority_level or obj.managed_job.operational_priority_level,
        )


class JobFileSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    notes = serializers.SerializerMethodField()

    class Meta:
        model = JobFile
        fields = [
            "id",
            "managed_job",
            "assignment",
            "file_type",
            "visibility",
            "status",
            "version",
            "original_filename",
            "notes",
            "created_at",
            "download_url",
        ]

    def get_download_url(self, obj):
        request = self.context.get("request")
        path = reverse("job-file-download", kwargs={"pk": obj.pk})
        if request:
            return request.build_absolute_uri(path)
        return path

    def get_notes(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor == OPS_ACTOR:
            return obj.notes
        if actor in {SHOP_ACTOR, PARTNER_ACTOR} and obj.visibility != "internal":
            return obj.notes
        return ""


class ManagedJobEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = ManagedJobEvent
        fields = [
            "id",
            "event_type",
            "summary",
            "metadata",
            "actor_name",
            "created_at",
        ]

    def get_actor_name(self, obj):
        if not obj.actor_id:
            return "System"
        return getattr(obj.actor, "name", "") or getattr(obj.actor, "email", "") or "User"


class JobActionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)


class JobPaymentSerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()
    external_reference = serializers.SerializerMethodField()
    expected_amount = serializers.SerializerMethodField()
    received_amount = serializers.SerializerMethodField()
    account_reference = serializers.SerializerMethodField()
    payer_phone = serializers.SerializerMethodField()
    checkout_request_id = serializers.SerializerMethodField()
    merchant_request_id = serializers.SerializerMethodField()
    mpesa_receipt_number = serializers.SerializerMethodField()

    class Meta:
        model = JobPayment
        fields = [
            "id",
            "managed_job",
            "amount",
            "expected_amount",
            "received_amount",
            "payment_method",
            "payment_channel",
            "payment_status",
            "reconciliation_status",
            "account_reference",
            "payer_phone",
            "checkout_request_id",
            "merchant_request_id",
            "mpesa_receipt_number",
            "external_reference",
            "confirmed_at",
            "created_at",
        ]

    def get_amount(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor in {OPS_ACTOR, SHOP_ACTOR, PARTNER_ACTOR, CLIENT_ACTOR}:
            return _money(obj.amount)
        return None

    def get_external_reference(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor == OPS_ACTOR:
            return obj.external_reference
        return ""

    def get_expected_amount(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor in {OPS_ACTOR, SHOP_ACTOR, PARTNER_ACTOR, CLIENT_ACTOR}:
            return _money(obj.expected_amount if obj.expected_amount is not None else obj.amount)
        return None

    def get_received_amount(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor in {OPS_ACTOR, SHOP_ACTOR, PARTNER_ACTOR, CLIENT_ACTOR}:
            return _money(obj.received_amount)
        return None

    def get_account_reference(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor in {OPS_ACTOR, CLIENT_ACTOR}:
            return obj.account_reference
        return ""

    def get_payer_phone(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor == OPS_ACTOR:
            return obj.payer_phone
        return ""

    def get_checkout_request_id(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor == OPS_ACTOR:
            return obj.checkout_request_id
        return ""

    def get_merchant_request_id(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor == OPS_ACTOR:
            return obj.merchant_request_id
        return ""

    def get_mpesa_receipt_number(self, obj):
        request = self.context.get("request")
        actor = resolve_actor(getattr(request, "user", None))
        if actor == OPS_ACTOR:
            return obj.mpesa_receipt_number
        return ""


class ManagedJobStkInitiateSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)


class JobPaymentQuerySerializer(serializers.Serializer):
    checkout_request_id = serializers.CharField(max_length=100)


class JobSettlementSplitSerializer(serializers.ModelSerializer):
    production_amount = serializers.SerializerMethodField()
    platform_fee = serializers.SerializerMethodField()
    partner_commission = serializers.SerializerMethodField()
    delivery_amount = serializers.SerializerMethodField()
    client_total = serializers.SerializerMethodField()
    relationship_owner_type = serializers.SerializerMethodField()
    relationship_owner_reference = serializers.SerializerMethodField()
    commission_recipient_type = serializers.SerializerMethodField()

    class Meta:
        model = JobSettlementSplit
        fields = [
            "id",
            "managed_job",
            "production_amount",
            "platform_fee",
            "partner_commission",
            "delivery_amount",
            "client_total",
            "relationship_owner_type",
            "relationship_owner_reference",
            "commission_recipient_type",
            "status",
            "payment_method",
            "release_ready_at",
            "released_at",
        ]

    def _actor(self) -> str:
        request = self.context.get("request")
        return resolve_actor(getattr(request, "user", None))

    def get_production_amount(self, obj):
        actor = self._actor()
        if actor in {OPS_ACTOR, SHOP_ACTOR}:
            return _money(obj.production_amount)
        return None

    def get_platform_fee(self, obj):
        actor = self._actor()
        if actor == OPS_ACTOR:
            return _money(obj.platform_fee)
        return None

    def get_partner_commission(self, obj):
        actor = self._actor()
        if actor in {OPS_ACTOR, PARTNER_ACTOR}:
            return _money(obj.partner_commission)
        return None

    def get_delivery_amount(self, obj):
        actor = self._actor()
        if actor in {OPS_ACTOR, CLIENT_ACTOR}:
            return _money(obj.delivery_amount)
        return None

    def get_client_total(self, obj):
        actor = self._actor()
        if actor in {OPS_ACTOR, CLIENT_ACTOR}:
            return _money(obj.client_total)
        return None

    def get_relationship_owner_type(self, obj):
        actor = self._actor()
        if actor == OPS_ACTOR:
            return obj.relationship_owner_type
        return ""

    def get_relationship_owner_reference(self, obj):
        actor = self._actor()
        if actor == OPS_ACTOR:
            return obj.relationship_owner_reference
        return ""

    def get_commission_recipient_type(self, obj):
        actor = self._actor()
        if actor in {OPS_ACTOR, PARTNER_ACTOR}:
            return obj.commission_recipient_type
        return ""
