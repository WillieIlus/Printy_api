from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.services.roles import (
    CANONICAL_CLIENT_ROLE,
    CANONICAL_PARTNER_ROLE,
    CANONICAL_PRODUCTION_ROLE,
    CANONICAL_SUPER_ADMIN_ROLE,
    is_super_admin,
    resolve_user_roles,
)
from api.services.admin_dashboard import build_admin_dashboard_payload
from api.visibility import project_shop_identity
from jobs.managed_services import create_assignment_for_managed_job
from jobs.models import JobAssignment, JobPayment, JobSettlementSplit, ManagedJob
from jobs.payment_services import calculate_partner_job_split, get_default_platform_service_percent
from jobs.serializers import JobSettlementSplitSerializer
from inventory.models import Paper
from notifications.models import Notification
from notifications.services import notify_quote_event
from pricing.models import FinishingRate, PrintingRate
from quotes.models import QuoteRequest, ShopQuote
from shops.models import Shop
from .models import PartnerClient
from .workflow_serializers import ClientQuoteRequestDetailSerializer, QuoteResponseReadSerializer


class PartnerClientCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=50)
    email = serializers.EmailField(required=False, allow_blank=True)
    company = serializers.CharField(required=False, allow_blank=True, max_length=255)


def _normalize_phone(value: str) -> str:
    return str(value or "").strip()


def _fallback_partner_client_email(*, partner_id: int, phone: str) -> str:
    digits = "".join(character for character in phone if character.isdigit()) or f"partner{partner_id}"
    return f"partner-client-{partner_id}-{digits}@printy.local"


def _partner_client_row(record: PartnerClient) -> dict[str, object]:
    client_user = getattr(record, "client_user", None)
    return {
        "id": record.id,
        "client_id": record.client_user_id,
        "name": record.name or getattr(client_user, "name", "") or getattr(client_user, "email", "") or "Client",
        "phone": record.phone or getattr(client_user, "username", "") or "",
        "email": record.email or getattr(client_user, "email", "") or "",
        "company": record.company or "",
        "is_new": False,
    }


class BaseDashboardHomeView(APIView):
    permission_classes = [IsAuthenticated]
    dashboard_role = ""
    allowed_roles: tuple[str, ...] = ()

    def has_dashboard_access(self, user) -> bool:
        roles = set(resolve_user_roles(user))
        return bool(roles.intersection(self.allowed_roles))

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.user and request.user.is_authenticated and not self.has_dashboard_access(request.user):
            raise PermissionDenied(
                detail={
                    "detail": f"This workspace is only available to {self.dashboard_role} accounts.",
                    "expected_dashboard_role": self.dashboard_role,
                }
            )


class ClientDashboardHomeView(BaseDashboardHomeView):
    dashboard_role = "client"
    allowed_roles = (CANONICAL_CLIENT_ROLE,)

    def get(self, request):
        jobs = ManagedJob.objects.filter(
            Q(client=request.user) | Q(created_by=request.user)
        ).select_related("assigned_shop").order_by("-updated_at", "-created_at").distinct()
        payments = JobPayment.objects.filter(managed_job__in=jobs).order_by("-created_at")
        recent_jobs = [
            {
                "id": job.id,
                "reference": job.managed_reference,
                "title": job.title or "Print job",
                "status": job.status,
                "payment_status": job.payment_status,
                "assigned_shop_name": project_shop_identity(
                    getattr(job.assigned_shop, "name", ""),
                    actor="client",
                    topology_mode="managed",
                ),
                "client_total": str(job.client_total) if job.client_total is not None else None,
            }
            for job in jobs[:6]
        ]
        payment_rows = [
            {
                "id": payment.id,
                "managed_job_id": payment.managed_job_id,
                "reference": getattr(payment.managed_job, "managed_reference", ""),
                "amount": str(payment.amount) if payment.amount is not None else None,
                "payment_status": payment.payment_status,
                "method": payment.method,
                "channel": payment.channel,
                "checkout_request_id": payment.checkout_request_id,
            }
            for payment in payments.select_related("managed_job")[:6]
        ]
        return Response(
            {
                "role": "client",
                "stats": {
                    "open_jobs": jobs.exclude(status__in=["completed", "cancelled"]).count(),
                    "awaiting_payment": jobs.filter(payment_status__in=["pending", "initiated"]).count(),
                    "in_production": jobs.filter(status__in=["accepted", "in_production", "ready"]).count(),
                },
                "recent_jobs": recent_jobs,
                "payments": payment_rows,
                "actions": {
                    "primary": "/quotes",
                    "secondary": "/dashboard/client#payments",
                },
            }
        )


class PartnerDashboardHomeView(BaseDashboardHomeView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get(self, request):
        jobs = ManagedJob.objects.filter(broker=request.user).select_related(
            "client",
            "assigned_shop",
        ).order_by("-updated_at", "-created_at")
        quote_requests = QuoteRequest.objects.filter(
            Q(created_by=request.user) | Q(managed_jobs__broker=request.user)
        ).select_related("shop").order_by("-updated_at", "-created_at").distinct()
        recent_jobs = [
            {
                "id": job.id,
                "reference": job.managed_reference,
                "title": job.title or "Managed print job",
                "status": job.status,
                "payment_status": job.payment_status,
                "assignment_status": job.assignment_status,
                "client_name": getattr(job.client, "name", "") or getattr(job.client, "email", "") or "Client",
                "client_total": str(job.client_total) if job.client_total is not None else None,
                "assigned_shop_name": getattr(job.assigned_shop, "name", "") or "Awaiting assignment",
            }
            for job in jobs[:8]
        ]
        request_rows = [
            {
                "id": quote_request.id,
                "reference": quote_request.request_reference or f"QR-{quote_request.id}",
                "status": quote_request.status,
                "customer_name": quote_request.customer_name or "Client",
                "shop_name": getattr(quote_request.shop, "name", "") or "Shop",
            }
            for quote_request in quote_requests[:8]
        ]
        return Response(
            {
                "role": "partner",
                "stats": {
                    "active_clients": jobs.exclude(client_id__isnull=True).values("client_id").distinct().count(),
                    "managed_jobs": jobs.count(),
                    "awaiting_client_payment": jobs.filter(payment_status__in=["pending", "initiated"]).count(),
                },
                "recent_jobs": recent_jobs,
                "quote_requests": request_rows,
                "actions": {
                    "primary": "/quotes",
                    "secondary": "/for-shops",
                },
            }
        )


class ProductionDashboardHomeView(BaseDashboardHomeView):
    dashboard_role = "production"
    allowed_roles = (CANONICAL_PRODUCTION_ROLE,)

    def get(self, request):
        assignments = JobAssignment.objects.filter(
            assigned_shop__owner=request.user,
            reassigned_from__isnull=True,
        ).select_related("managed_job", "assigned_shop").order_by("-operational_priority_level", "-id")
        jobs = ManagedJob.objects.filter(assigned_shop__owner=request.user).select_related("assigned_shop").order_by(
            "-operational_priority_level",
            "-updated_at",
        )
        recent_assignments = [
            {
                "id": assignment.id,
                "managed_job_id": assignment.managed_job_id,
                "reference": getattr(assignment.managed_job, "managed_reference", ""),
                "status": assignment.status,
                "managed_job_status": getattr(assignment.managed_job, "status", ""),
                "payment_status": getattr(assignment.managed_job, "payment_status", ""),
                "priority": assignment.operational_priority_level,
                "due_at": assignment.due_at,
            }
            for assignment in assignments[:8]
        ]
        queue_rows = [
            {
                "id": job.id,
                "reference": job.managed_reference,
                "title": job.title or "Production job",
                "status": job.status,
                "payment_status": job.payment_status,
                "assignment_status": job.assignment_status,
            }
            for job in jobs[:8]
        ]
        return Response(
            {
                "role": "production",
                "stats": {
                    "incoming_assignments": assignments.filter(status="pending").count(),
                    "in_production": assignments.filter(status__in=["accepted", "in_production"]).count(),
                    "payment_holds": jobs.filter(payment_status__in=["pending", "initiated"]).count(),
                },
                "assignments": recent_assignments,
                "queue": queue_rows,
                "actions": {
                    "primary": "/dashboard/production#assignments",
                    "secondary": "/shop/jobs/incoming",
                },
            }
        )


class AdminDashboardHomeView(BaseDashboardHomeView):
    dashboard_role = "super_admin"
    allowed_roles = (CANONICAL_SUPER_ADMIN_ROLE,)

    def has_dashboard_access(self, user) -> bool:
        return is_super_admin(user)

    def get(self, request):
        return Response(build_admin_dashboard_payload())


def _production_shop_filter(user):
    return Q(assigned_shop__owner=user) | Q(assigned_shop__memberships__user=user, assigned_shop__memberships__is_active=True)


def _job_pricing_snapshot(job: ManagedJob, role: str) -> dict[str, str | None]:
    client_total = str(job.client_total) if job.client_total is not None else None
    production_total = str(job.production_total) if job.production_total is not None else None
    partner_commission = str(job.broker_commission) if job.broker_commission is not None else None
    if role == CANONICAL_PRODUCTION_ROLE:
        return {
            "production_total": production_total,
            "paper_price": None,
            "finishing_price": None,
            "client_total": None,
            "partner_commission": None,
            "service_fee": None,
        }
    if role == CANONICAL_PARTNER_ROLE:
        service_fee = None
        if job.client_total is not None and job.production_total is not None and job.broker_commission is not None:
            service_fee = str(job.client_total - job.production_total - job.broker_commission)
        return {
            "production_total": production_total,
            "client_total": client_total,
            "partner_commission": partner_commission,
            "service_fee": service_fee,
        }
    return {
        "client_total": client_total,
        "service_fee": None,
    }


class BaseRoleDetailView(BaseDashboardHomeView):
    def _client_tracking_payload(self, job: ManagedJob | None) -> dict[str, object | None]:
        if not job:
            return {
                "tracking_token": None,
                "public_token": None,
            }
        return {
            "tracking_token": str(job.tracking_token) if getattr(job, "tracking_token", None) else None,
            "public_token": None,
        }

    def _quote_row(self, quote_request: QuoteRequest) -> dict[str, object]:
        row = {
            "id": quote_request.id,
            "reference": quote_request.request_reference or f"QR-{quote_request.id}",
            "status": quote_request.status,
            "customer_name": quote_request.customer_name or "Client",
            "shop_name": getattr(quote_request.shop, "name", "") or "Shop",
            "created_at": quote_request.created_at,
            "updated_at": quote_request.updated_at,
        }
        managed_job = quote_request.managed_jobs.order_by("-id").first()
        row["managed_job"] = {
            "id": managed_job.id,
            **self._client_tracking_payload(managed_job),
        } if managed_job else None
        return row

    def _job_row(self, job: ManagedJob, *, role: str) -> dict[str, object]:
        row = {
            "id": job.id,
            "reference": job.managed_reference,
            "title": job.title or "Managed print job",
            "status": job.status,
            "payment_status": job.payment_status,
            "assignment_status": job.assignment_status,
            "requested_deadline": job.requested_deadline,
            "updated_at": job.updated_at,
            "pricing": _job_pricing_snapshot(job, role),
        }
        if role == CANONICAL_CLIENT_ROLE:
            row.update(self._client_tracking_payload(job))
        if role == CANONICAL_PARTNER_ROLE:
            row["client_name"] = getattr(job.client, "name", "") or getattr(job.client, "email", "") or "Client"
            row["assigned_shop_name"] = getattr(job.assigned_shop, "name", "") or "Awaiting assignment"
        if role == CANONICAL_PRODUCTION_ROLE:
            row["assigned_shop_name"] = getattr(job.assigned_shop, "name", "") or "Production Shop"
        return row


class ClientQuoteListView(BaseRoleDetailView):
    dashboard_role = "client"
    allowed_roles = (CANONICAL_CLIENT_ROLE,)

    def get(self, request):
        rows = QuoteRequest.objects.filter(created_by=request.user).select_related("shop").order_by("-updated_at", "-created_at")
        return Response({"role": "client", "results": [self._quote_row(item) for item in rows]})


class ClientQuoteDetailView(BaseRoleDetailView):
    dashboard_role = "client"
    allowed_roles = (CANONICAL_CLIENT_ROLE,)

    def get(self, request, pk):
        quote_request = get_object_or_404(
            QuoteRequest.objects.select_related("shop", "on_behalf_of"),
            Q(created_by=request.user) | Q(on_behalf_of=request.user),
            pk=pk,
        )
        return Response({"role": "client", "quote": ClientQuoteRequestDetailSerializer(quote_request, context={"request": request}).data})


class ClientJobListView(BaseRoleDetailView):
    dashboard_role = "client"
    allowed_roles = (CANONICAL_CLIENT_ROLE,)

    def get(self, request):
        jobs = ManagedJob.objects.filter(Q(client=request.user) | Q(created_by=request.user)).select_related("assigned_shop").order_by("-updated_at", "-created_at").distinct()
        return Response({"role": "client", "results": [self._job_row(job, role=CANONICAL_CLIENT_ROLE) for job in jobs]})


class ClientJobDetailView(BaseRoleDetailView):
    dashboard_role = "client"
    allowed_roles = (CANONICAL_CLIENT_ROLE,)

    def get(self, request, pk):
        job = get_object_or_404(
            ManagedJob.objects.select_related("assigned_shop"),
            Q(client=request.user) | Q(created_by=request.user),
            pk=pk,
        )
        return Response(
            {
                "role": "client",
                "job": self._job_row(job, role=CANONICAL_CLIENT_ROLE),
                "settlement": None,
            }
        )


class ClientPaymentListView(BaseRoleDetailView):
    dashboard_role = "client"
    allowed_roles = (CANONICAL_CLIENT_ROLE,)

    def get(self, request):
        jobs = ManagedJob.objects.filter(Q(client=request.user) | Q(created_by=request.user))
        payments = JobPayment.objects.filter(managed_job__in=jobs).select_related("managed_job").order_by("-created_at")
        return Response(
            {
                "role": "client",
                "results": [
                    {
                        "id": payment.id,
                        "reference": getattr(payment.managed_job, "managed_reference", ""),
                        "amount": str(payment.amount) if payment.amount is not None else None,
                        "payment_status": payment.payment_status,
                        "channel": payment.channel,
                        "created_at": payment.created_at,
                    }
                    for payment in payments
                ],
            }
        )


class PartnerQuoteListDetailView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get_queryset(self, request):
        return QuoteRequest.objects.filter(Q(created_by=request.user) | Q(managed_jobs__broker=request.user)).select_related("shop").distinct().order_by("-updated_at", "-created_at")

    def get(self, request, pk=None):
        if pk is not None:
            quote_request = self.get_queryset(request).get(pk=pk)
            latest_response = quote_request.shop_quotes.exclude(status=ShopQuote.PENDING).select_related("shop").order_by("-created_at", "-id").first()
            payload = self._quote_row(quote_request)
            payload["client_name"] = quote_request.customer_name or getattr(quote_request.on_behalf_of, "name", "") or "Client"
            payload["client_email"] = quote_request.customer_email or getattr(quote_request.on_behalf_of, "email", "")
            payload["client_phone"] = quote_request.customer_phone
            payload["on_behalf_of_user_id"] = quote_request.on_behalf_of_id
            payload["latest_response"] = QuoteResponseReadSerializer(latest_response, context={"request": request}).data if latest_response else None
            managed_job = quote_request.managed_jobs.select_related("assigned_shop").order_by("-id").first()
            if managed_job:
                payload["managed_job"] = self._job_row(managed_job, role=CANONICAL_PARTNER_ROLE)
            return Response({"role": "partner", "quote": payload})
        return Response({"role": "partner", "results": [self._quote_row(item) for item in self.get_queryset(request)]})


class PartnerQuoteSendToClientView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get_queryset(self, request):
        return QuoteRequest.objects.filter(
            Q(created_by=request.user) | Q(managed_jobs__broker=request.user)
        ).select_related("shop", "on_behalf_of").distinct()

    def post(self, request, pk):
        quote_request = get_object_or_404(self.get_queryset(request), pk=pk)
        if quote_request.on_behalf_of_id is None:
            return Response({"detail": "client_id is required for partner quote requests."}, status=400)
        latest_response = quote_request.shop_quotes.exclude(status=ShopQuote.PENDING).order_by("-created_at", "-id").first()
        if latest_response is None or latest_response.total is None:
            return Response({"detail": "A production base quote is required before sending to the client."}, status=400)

        broker_margin_type = str(request.data.get("broker_margin_type") or "percent").strip().lower()
        broker_margin_value = Decimal(str(request.data.get("broker_margin_value") or "30"))
        platform_service_percent = Decimal(
            str(request.data.get("platform_service_percent") or get_default_platform_service_percent())
        )
        base_price = Decimal(str(latest_response.total))

        if broker_margin_type == "fixed":
            broker_margin_amount = broker_margin_value.quantize(Decimal("0.01"))
            broker_margin_percent = (
                (broker_margin_amount / base_price) * Decimal("100")
            ).quantize(Decimal("0.01")) if base_price > 0 else Decimal("0.00")
            platform_service_amount = (base_price * platform_service_percent / Decimal("100")).quantize(Decimal("0.01"))
            split = {
                "production_amount": base_price.quantize(Decimal("0.01")),
                "broker_margin_percent": broker_margin_percent,
                "broker_margin_amount": broker_margin_amount,
                "platform_service_percent": platform_service_percent.quantize(Decimal("0.01")),
                "platform_service_amount": platform_service_amount,
                "client_total": (base_price + broker_margin_amount + platform_service_amount).quantize(Decimal("0.01")),
            }
        else:
            split = calculate_partner_job_split(
                base_price,
                broker_margin_percent=broker_margin_value,
                platform_service_percent=platform_service_percent,
            )

        response_snapshot = dict(latest_response.response_snapshot or {})
        response_snapshot["customer_pricing"] = {
            "production_base_price": str(split["production_amount"]),
            "broker_margin_type": broker_margin_type,
            "broker_margin_percent": str(split["broker_margin_percent"]),
            "broker_margin_amount": str(split["broker_margin_amount"]),
            "platform_service_percent": str(split["platform_service_percent"]),
            "platform_service_amount": str(split["platform_service_amount"]),
            "final_client_price": str(split["client_total"]),
            "service_fee_public": True,
        }
        response_snapshot["pricing"] = {**dict(response_snapshot.get("pricing") or {}), "grand_total": str(split["client_total"])}
        response_snapshot["totals"] = {**dict(response_snapshot.get("totals") or {}), "grand_total": str(split["client_total"])}
        latest_response.response_snapshot = response_snapshot
        latest_response.sent_at = latest_response.sent_at or timezone.now()
        latest_response.production_base_price = split["production_amount"]
        latest_response.broker_margin_type = broker_margin_type
        latest_response.broker_margin_value = broker_margin_value.quantize(Decimal("0.01"))
        latest_response.broker_margin_amount = split["broker_margin_amount"]
        latest_response.platform_service_percent = split["platform_service_percent"]
        latest_response.platform_service_amount = split["platform_service_amount"]
        latest_response.client_total = split["client_total"]
        latest_response.sent_to_client_at = timezone.now()
        latest_response.sent_to_client_by = request.user
        latest_response.client_quote_status = "sent"
        latest_response.save(
            update_fields=[
                "response_snapshot",
                "sent_at",
                "production_base_price",
                "broker_margin_type",
                "broker_margin_value",
                "broker_margin_amount",
                "platform_service_percent",
                "platform_service_amount",
                "client_total",
                "sent_to_client_at",
                "sent_to_client_by",
                "client_quote_status",
                "updated_at",
            ]
        )

        request_snapshot = dict(quote_request.request_snapshot or {})
        request_snapshot["customer_pricing"] = response_snapshot["customer_pricing"]
        quote_request.request_snapshot = request_snapshot
        quote_request.save(update_fields=["request_snapshot", "updated_at"])

        return Response(
            {
                "quote_request_id": quote_request.id,
                "shop_quote_id": latest_response.id,
                "pricing": response_snapshot["customer_pricing"],
            }
        )


class PartnerJobListDetailView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get_queryset(self, request):
        return ManagedJob.objects.filter(broker=request.user).select_related("client", "assigned_shop").order_by("-updated_at", "-created_at")

    def get(self, request, pk=None):
        if pk is not None:
            job = self.get_queryset(request).get(pk=pk)
            settlement = JobSettlementSplit.objects.filter(managed_job=job).order_by("-id").first()
            return Response(
                {
                    "role": "partner",
                    "job": self._job_row(job, role=CANONICAL_PARTNER_ROLE),
                    "settlement": JobSettlementSplitSerializer(settlement, context={"request": request}).data if settlement else None,
                }
            )
        return Response({"role": "partner", "results": [self._job_row(job, role=CANONICAL_PARTNER_ROLE) for job in self.get_queryset(request)]})


class PartnerJobDispatchView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get_queryset(self, request):
        return ManagedJob.objects.filter(broker=request.user).select_related(
            "client",
            "assigned_shop",
            "source_shop_quote",
            "source_shop_quote__shop",
            "source_shop_quote__shop__owner",
        )

    def post(self, request, pk):
        job = get_object_or_404(self.get_queryset(request), pk=pk)
        if job.payment_status not in {"confirmed", "release_ready"} and job.status != "payment_confirmed":
            return Response({"detail": "Client payment must be confirmed before dispatch."}, status=400)
        if job.dispatched_at is not None:
            return Response({"detail": "This job has already been dispatched."}, status=400)

        source_shop_quote = job.source_shop_quote
        if source_shop_quote is None:
            return Response({"detail": "This job has no source production quote to dispatch."}, status=400)

        job.assigned_shop = source_shop_quote.shop
        job.dispatched_at = timezone.now()
        job.dispatched_by = request.user
        if job.assignment_status == "unassigned":
            job.assignment_status = "assignment_pending"
        job.save(update_fields=["assigned_shop", "dispatched_at", "dispatched_by", "assignment_status", "updated_at"])
        assignment = create_assignment_for_managed_job(managed_job=job, shop_quote=source_shop_quote)
        production_recipient = getattr(source_shop_quote.shop, "owner", None)
        if production_recipient and getattr(production_recipient, "id", None) != request.user.id:
            notify_quote_event(
                recipient=production_recipient,
                notification_type=Notification.JOB_STATUS_UPDATED,
                message=f"{job.managed_reference or 'Managed job'} has been dispatched to your production queue.",
                object_type="managed_job",
                object_id=job.id,
                actor=request.user,
            )
        return Response(
            {
                "job_id": job.id,
                "assignment_id": assignment.id,
                "dispatched_at": job.dispatched_at,
                "assignment_status": job.assignment_status,
            }
        )


class PartnerClientListView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get(self, request):
        rows: list[dict[str, object]] = []
        seen_client_ids: set[int] = set()

        saved_clients = PartnerClient.objects.filter(partner=request.user).select_related("client_user").order_by("name", "id")
        for record in saved_clients:
            if record.client_user_id:
                seen_client_ids.add(record.client_user_id)
            rows.append(_partner_client_row(record))

        jobs = ManagedJob.objects.filter(broker=request.user, client_id__isnull=False).select_related("client")
        for job in jobs:
            if not job.client_id or job.client_id in seen_client_ids:
                continue
            seen_client_ids.add(job.client_id)
            rows.append(
                {
                    "id": job.client_id,
                    "client_id": job.client_id,
                    "name": getattr(job.client, "name", "") or getattr(job.client, "email", "") or "Client",
                    "phone": getattr(job.client, "username", "") or "",
                    "email": getattr(job.client, "email", "") or "",
                    "company": "",
                    "is_new": False,
                }
            )
        return Response({"role": "partner", "results": rows})

    @transaction.atomic
    def post(self, request):
        serializer = PartnerClientCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        User = get_user_model()
        name = serializer.validated_data["name"].strip()
        phone = _normalize_phone(serializer.validated_data["phone"])
        email = serializer.validated_data.get("email", "").strip().lower()
        company = serializer.validated_data.get("company", "").strip()

        existing_record = PartnerClient.objects.filter(partner=request.user).select_related("client_user").filter(
            Q(phone=phone) | (Q(email=email) if email else Q(pk__in=[]))
        ).order_by("id").first()
        if existing_record:
            update_fields: list[str] = []
            if name and existing_record.name != name:
                existing_record.name = name
                update_fields.append("name")
            if email and existing_record.email != email:
                existing_record.email = email
                update_fields.append("email")
            if company != existing_record.company:
                existing_record.company = company
                update_fields.append("company")
            if update_fields:
                update_fields.append("updated_at")
                existing_record.save(update_fields=update_fields)
            return Response(
                {
                    "client_id": existing_record.client_user_id,
                    "name": existing_record.name,
                    "phone": existing_record.phone,
                    "email": existing_record.email,
                    "company": existing_record.company,
                    "is_new": False,
                },
                status=200,
            )

        resolved_user = None
        if phone:
            resolved_user = User.objects.filter(username=phone).first()
        if resolved_user is None and email:
            resolved_user = User.objects.filter(email__iexact=email).first()

        if resolved_user is not None:
            if getattr(resolved_user, "role", "") != User.Role.CLIENT:
                return Response({"detail": "Existing account cannot be linked as a partner client."}, status=400)
            is_new = False
        else:
            fallback_email = email or _fallback_partner_client_email(partner_id=request.user.id, phone=phone)
            resolved_user = User.objects.create_user(
                email=fallback_email,
                password=None,
                username=phone,
                name=name,
                role=User.Role.CLIENT,
                is_active=True,
            )
            is_new = True

        record, created = PartnerClient.objects.get_or_create(
            partner=request.user,
            client_user=resolved_user,
            defaults={
                "name": name or getattr(resolved_user, "name", "") or getattr(resolved_user, "email", "") or "Client",
                "phone": phone,
                "email": email or getattr(resolved_user, "email", "") or "",
                "company": company,
            },
        )
        update_fields: list[str] = []
        if name and record.name != name:
            record.name = name
            update_fields.append("name")
        if phone and record.phone != phone:
            record.phone = phone
            update_fields.append("phone")
        if email and record.email != email:
            record.email = email
            update_fields.append("email")
        if company != record.company:
            record.company = company
            update_fields.append("company")
        if update_fields:
            update_fields.append("updated_at")
            record.save(update_fields=update_fields)

        return Response(
            {
                "client_id": resolved_user.id,
                "name": record.name,
                "phone": record.phone,
                "email": record.email,
                "company": record.company,
                "is_new": is_new and created,
            },
            status=201 if is_new and created else 200,
        )


class PartnerProductionShopListView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get(self, request):
        shops = Shop.objects.filter(managed_jobs__broker=request.user).distinct().order_by("name")
        return Response({"role": "partner", "results": [{"id": shop.id, "name": shop.name, "slug": shop.slug} for shop in shops]})


class PartnerPaymentListView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get(self, request):
        jobs = ManagedJob.objects.filter(broker=request.user)
        settlements = JobSettlementSplit.objects.filter(managed_job__in=jobs).select_related("managed_job").order_by("-id")
        return Response(
            {
                "role": "partner",
                "results": [
                    {
                        "id": settlement.id,
                        "managed_job_id": settlement.managed_job_id,
                        "reference": getattr(settlement.managed_job, "managed_reference", ""),
                        "partner_commission": str(settlement.partner_commission),
                        "status": settlement.status,
                        "payment_method": settlement.payment_method,
                    }
                    for settlement in settlements
                ],
            }
        )


class ProductionJobListDetailView(BaseRoleDetailView):
    dashboard_role = "production"
    allowed_roles = (CANONICAL_PRODUCTION_ROLE,)

    def get_queryset(self, request):
        return ManagedJob.objects.filter(_production_shop_filter(request.user)).select_related("assigned_shop").distinct().order_by("-operational_priority_level", "-updated_at")

    def get(self, request, pk=None):
        if pk is not None:
            job = self.get_queryset(request).get(pk=pk)
            settlement = JobSettlementSplit.objects.filter(managed_job=job).order_by("-id").first()
            return Response(
                {
                    "role": "production",
                    "job": self._job_row(job, role=CANONICAL_PRODUCTION_ROLE),
                    "settlement": JobSettlementSplitSerializer(settlement, context={"request": request}).data if settlement else None,
                }
            )
        return Response({"role": "production", "results": [self._job_row(job, role=CANONICAL_PRODUCTION_ROLE) for job in self.get_queryset(request)]})


class ProductionPricingListView(BaseRoleDetailView):
    dashboard_role = "production"
    allowed_roles = (CANONICAL_PRODUCTION_ROLE,)

    def get(self, request):
        pricing_rows = PrintingRate.objects.filter(shop__owner=request.user).select_related("shop").order_by("shop__name", "sheet_size")[:100]
        return Response(
            {
                "role": "production",
                "results": [
                    {
                        "id": row.id,
                        "shop_name": row.shop.name,
                        "sheet_size": row.sheet_size,
                        "color_mode": row.color_mode,
                        "single_price": str(row.single_price),
                        "double_price": str(row.double_price) if row.double_price is not None else None,
                    }
                    for row in pricing_rows
                ],
            }
        )


class ProductionPaperStockListView(BaseRoleDetailView):
    dashboard_role = "production"
    allowed_roles = (CANONICAL_PRODUCTION_ROLE,)

    def get(self, request):
        papers = Paper.objects.filter(shop__owner=request.user).select_related("shop").order_by("shop__name", "paper_type")[:100]
        return Response(
            {
                "role": "production",
                "results": [
                    {
                        "id": paper.id,
                        "shop_name": paper.shop.name,
                        "paper_type": getattr(paper, "paper_type", ""),
                        "name": getattr(paper, "display_name", "") or getattr(paper, "name", ""),
                        "gsm": paper.gsm,
                        "sheet_size": paper.sheet_size,
                        "is_active": paper.is_active,
                    }
                    for paper in papers
                ],
            }
        )


class ProductionFinishingListView(BaseRoleDetailView):
    dashboard_role = "production"
    allowed_roles = (CANONICAL_PRODUCTION_ROLE,)

    def get(self, request):
        finishings = FinishingRate.objects.filter(shop__owner=request.user).select_related("shop").order_by("shop__name", "name")[:100]
        return Response(
            {
                "role": "production",
                "results": [
                    {
                        "id": item.id,
                        "shop_name": item.shop.name,
                        "name": item.name,
                        "unit": item.charge_unit,
                        "price": str(item.price),
                        "is_active": item.is_active,
                    }
                    for item in finishings
                ],
            }
        )


class ProductionPaymentListView(BaseRoleDetailView):
    dashboard_role = "production"
    allowed_roles = (CANONICAL_PRODUCTION_ROLE,)

    def get(self, request):
        jobs = ManagedJob.objects.filter(_production_shop_filter(request.user)).distinct()
        settlements = JobSettlementSplit.objects.filter(managed_job__in=jobs).select_related("managed_job").order_by("-id")
        return Response(
            {
                "role": "production",
                "results": [
                    {
                        "id": settlement.id,
                        "managed_job_id": settlement.managed_job_id,
                        "reference": getattr(settlement.managed_job, "managed_reference", ""),
                        "production_amount": str(settlement.production_amount),
                        "status": settlement.status,
                        "payment_method": settlement.payment_method,
                    }
                    for settlement in settlements
                ],
            }
        )
