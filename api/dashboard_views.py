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
from accounts.models import UserProfile
from api.services.admin_dashboard import build_admin_dashboard_payload
from api.visibility import project_shop_identity
from jobs.managed_services import create_assignment_for_managed_job
from jobs.models import JobAssignment, JobPayment, JobSettlementSplit, ManagedJob
from jobs.payment_services import calculate_partner_job_split, get_default_platform_service_percent
from jobs.serializers import JobSettlementSplitSerializer
from inventory.models import Paper
from notifications.models import Notification
from notifications.services import notify_quote_event
from jobs.file_services import managed_job_has_artwork, notify_missing_artwork
from pricing.models import FinishingRate, PrintingRate
from quotes.models import QuoteRequest, ShopQuote
from quotes.partner_services import respond_to_assigned_quote_request
from quotes.services_workflow import update_quote_response
from services.production_matching import build_partner_production_matches
from services.pricing.partner_market_rates import build_partner_market_rate_payload
from shops.models import Shop
from .models import PartnerClient
from .workflow_serializers import (
    ClientQuoteRequestDetailSerializer,
    PartnerAssignedRequestShopOptionsSerializer,
    PartnerQuoteAttachClientSerializer,
    PartnerProductionMatchResponseSerializer,
    PartnerQuotePreviewSerializer,
    QuoteRequestReadSerializer,
    QuoteResponseReadSerializer,
)


class PartnerClientCreateSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=50)
    email = serializers.EmailField(required=False, allow_blank=True)
    company = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        if not attrs.get("email") and not attrs.get("phone") and not attrs.get("name"):
            raise serializers.ValidationError("Email, phone, or name is required.")
        if not attrs.get("email") and not attrs.get("phone"):
            raise serializers.ValidationError("Email or phone is required to create a partner client.")
        return attrs


def _normalize_phone(value: str) -> str:
    return str(value or "").strip()


def _fallback_partner_client_email(*, partner_id: int, phone: str) -> str:
    digits = "".join(character for character in phone if character.isdigit()) or f"partner{partner_id}"
    return f"partner-client-{partner_id}-{digits}@printy.local"


def _partner_client_username(*, phone: str, email: str, partner_id: int) -> str:
    return phone or email or f"partner-client-{partner_id}"


def _resolve_or_create_partner_client(
    *,
    partner_user,
    client_user=None,
    client_name: str = "",
    client_email: str = "",
    client_phone: str = "",
    client_company: str = "",
):
    User = get_user_model()
    name = str(client_name or "").strip()
    phone = _normalize_phone(client_phone)
    email = str(client_email or "").strip().lower()
    company = str(client_company or "").strip()

    resolved_user = client_user
    if resolved_user is None and phone:
        resolved_user = User.objects.filter(username=phone).first()
    if resolved_user is None and email:
        resolved_user = User.objects.filter(email__iexact=email).first()

    if resolved_user is not None and getattr(resolved_user, "role", "") != User.Role.CLIENT:
        raise ValueError("Existing account cannot be linked as a partner client.")

    created_user = False
    if resolved_user is None:
        if not email:
            raise ValueError("Client email is required when no existing client is selected.")
        fallback_email = email or _fallback_partner_client_email(partner_id=partner_user.id, phone=phone)
        resolved_user = User.objects.create_user(
            email=fallback_email,
            password=None,
            username=_partner_client_username(phone=phone, email=fallback_email, partner_id=partner_user.id),
            name=name or fallback_email or phone or "Client",
            role=User.Role.CLIENT,
            is_active=True,
        )
        created_user = True

    record, created_record = PartnerClient.objects.get_or_create(
        partner=partner_user,
        client_user=resolved_user,
        defaults={
            "name": name or getattr(resolved_user, "name", "") or getattr(resolved_user, "email", "") or "Client",
            "phone": phone,
            "email": email or getattr(resolved_user, "email", "") or "",
            "company": company,
        },
    )
    update_fields: list[str] = []
    desired_name = name or record.name or getattr(resolved_user, "name", "") or getattr(resolved_user, "email", "") or "Client"
    if desired_name and record.name != desired_name:
        record.name = desired_name
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

    return {
        "client_user": resolved_user,
        "client_id": resolved_user.id,
        "name": record.name,
        "phone": record.phone,
        "email": record.email,
        "company": record.company,
        "is_new": created_user and created_record,
    }


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


class PartnerDashboardProfileSerializer(serializers.Serializer):
    default_markup_rate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal("0.00"), max_value=Decimal("5.00"))


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
            Q(created_by=request.user) | Q(assigned_manager=request.user) | Q(managed_jobs__broker=request.user)
        ).select_related("shop", "assigned_manager").order_by("-updated_at", "-created_at").distinct()
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
                "shop_name": getattr(quote_request.shop, "name", "") or "Awaiting production match",
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


class PartnerMarketRateListView(BaseDashboardHomeView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get(self, request):
        return Response(build_partner_market_rate_payload(user=request.user))


class PartnerDashboardProfileView(BaseDashboardHomeView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def _get_profile(self, user):
        profile, _ = UserProfile.objects.get_or_create(user=user)
        return profile

    def get(self, request):
        profile = self._get_profile(request.user)
        return Response({"default_markup_rate": str(profile.default_markup_rate)})

    def patch(self, request):
        serializer = PartnerDashboardProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = self._get_profile(request.user)
        profile.default_markup_rate = serializer.validated_data["default_markup_rate"]
        profile.save(update_fields=["default_markup_rate", "updated_at"])
        return Response({"default_markup_rate": str(profile.default_markup_rate)})


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
    def _has_artwork(self, job: ManagedJob) -> bool:
        return job.job_files.filter(file_type__in=["artwork", "customer_upload"]).exists()

    def _request_snapshot_root(self, quote_request: QuoteRequest | None) -> dict[str, object]:
        return quote_request.request_snapshot if quote_request and isinstance(quote_request.request_snapshot, dict) else {}

    def _request_snapshot(self, quote_request: QuoteRequest | None) -> dict[str, object]:
        if not quote_request:
            return {}
        snapshot = self._request_snapshot_root(quote_request)
        nested = snapshot.get("request_snapshot")
        if isinstance(nested, dict):
            return nested
        return snapshot

    def _assigned_request_match_payload(self, quote_request: QuoteRequest, overrides: dict[str, object] | None = None) -> dict[str, object]:
        snapshot = self._request_snapshot_root(quote_request)
        nested = self._request_snapshot(quote_request)
        calculator_inputs = snapshot.get("calculator_inputs") if isinstance(snapshot.get("calculator_inputs"), dict) else {}
        overrides = overrides or {}

        def _pick(*values):
            for value in values:
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                return value
            return None

        payload = {
            "product_type": _pick(overrides.get("product_type"), nested.get("product_type"), calculator_inputs.get("product_type")),
            "quantity": _pick(overrides.get("quantity"), nested.get("quantity"), calculator_inputs.get("quantity")),
            "finished_size": _pick(overrides.get("finished_size"), nested.get("finished_size"), nested.get("size_label"), calculator_inputs.get("finished_size")),
            "paper_stock": _pick(overrides.get("paper_stock"), nested.get("paper_stock"), calculator_inputs.get("paper_stock")),
            "print_sides": _pick(overrides.get("print_sides"), nested.get("print_sides"), calculator_inputs.get("print_sides")),
            "color_mode": _pick(overrides.get("color_mode"), nested.get("color_mode"), calculator_inputs.get("color_mode")),
            "lamination": _pick(overrides.get("lamination"), nested.get("lamination"), calculator_inputs.get("lamination")),
            "urgency_type": _pick(overrides.get("urgency_type"), nested.get("urgency_type"), calculator_inputs.get("urgency_type")),
            "requested_paper_category": _pick(overrides.get("requested_paper_category"), nested.get("requested_paper_category"), calculator_inputs.get("requested_paper_category")),
            "requested_gsm": _pick(overrides.get("requested_gsm"), nested.get("requested_gsm"), calculator_inputs.get("requested_gsm")),
            "total_pages": _pick(overrides.get("total_pages"), nested.get("total_pages"), calculator_inputs.get("total_pages")),
            "cover_stock": _pick(overrides.get("cover_stock"), nested.get("cover_stock"), calculator_inputs.get("cover_stock")),
            "insert_stock": _pick(overrides.get("insert_stock"), nested.get("insert_stock"), calculator_inputs.get("insert_stock")),
            "requested_cover_paper_category": _pick(overrides.get("requested_cover_paper_category"), nested.get("requested_cover_paper_category"), calculator_inputs.get("requested_cover_paper_category")),
            "requested_cover_gsm": _pick(overrides.get("requested_cover_gsm"), nested.get("requested_cover_gsm"), calculator_inputs.get("requested_cover_gsm")),
            "requested_insert_paper_category": _pick(overrides.get("requested_insert_paper_category"), nested.get("requested_insert_paper_category"), calculator_inputs.get("requested_insert_paper_category")),
            "requested_insert_gsm": _pick(overrides.get("requested_insert_gsm"), nested.get("requested_insert_gsm"), calculator_inputs.get("requested_insert_gsm")),
            "cover_lamination": _pick(overrides.get("cover_lamination"), nested.get("cover_lamination"), calculator_inputs.get("cover_lamination")),
            "binding_type": _pick(overrides.get("binding_type"), nested.get("binding_type"), calculator_inputs.get("binding_type")),
            "material_type": _pick(overrides.get("material_type"), nested.get("material_type"), calculator_inputs.get("material_type")),
            "product_subtype": _pick(overrides.get("product_subtype"), nested.get("product_subtype"), calculator_inputs.get("product_subtype")),
            "width_mm": _pick(overrides.get("width_mm"), nested.get("width_mm"), calculator_inputs.get("width_mm"), nested.get("custom_width_mm"), calculator_inputs.get("custom_width_mm")),
            "height_mm": _pick(overrides.get("height_mm"), nested.get("height_mm"), calculator_inputs.get("height_mm"), nested.get("custom_height_mm"), calculator_inputs.get("custom_height_mm")),
        }
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    def _production_specs_snapshot(self, job: ManagedJob) -> dict[str, object]:
        request_snapshot = self._request_snapshot(getattr(job, "source_quote_request", None))
        operational_snapshot = job.operational_snapshot if isinstance(job.operational_snapshot, dict) else {}

        def _first_value(*values):
            for value in values:
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                return value
            return None

        def _label(raw):
            if raw is None:
                return None
            return str(raw).replace("_", " ").replace("-", " ").strip().title()

        paper_name = _first_value(request_snapshot.get("paper_label"), request_snapshot.get("paper_stock"))
        paper_gsm = request_snapshot.get("requested_gsm")
        paper_label = None
        if paper_name and paper_gsm:
            paper_label = f"{paper_name} ({paper_gsm}gsm)"
        elif paper_name:
            paper_label = str(paper_name)
        elif paper_gsm:
            paper_label = f"{paper_gsm}gsm stock"

        finishing = _first_value(request_snapshot.get("lamination_label"), _label(request_snapshot.get("lamination")))
        notes = _first_value(
            operational_snapshot.get("needs_confirmation"),
            request_snapshot.get("custom_brief"),
            getattr(getattr(job, "source_quote_request", None), "notes", ""),
        )
        if isinstance(notes, list):
            notes = ", ".join(str(item).strip() for item in notes if str(item).strip())

        return {
            "product": _first_value(
                request_snapshot.get("product_label"),
                _label(request_snapshot.get("product_type")),
                job.title,
            ),
            "quantity": request_snapshot.get("quantity"),
            "size": _first_value(request_snapshot.get("finished_size"), request_snapshot.get("size_label")),
            "paper": paper_label,
            "print_sides": _first_value(request_snapshot.get("print_sides_label"), _label(request_snapshot.get("print_sides"))),
            "color_mode": _first_value(request_snapshot.get("color_mode_label"), _label(request_snapshot.get("color_mode"))),
            "finishing": finishing,
            "notes": notes,
            "matched_specs": operational_snapshot.get("matched_specs") or [],
        }

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

    def _quote_row(self, quote_request: QuoteRequest, *, request=None) -> dict[str, object]:
        serialized = QuoteRequestReadSerializer(quote_request, context={"request": request}).data
        row = {
            "id": quote_request.id,
            "reference": quote_request.request_reference or f"QR-{quote_request.id}",
            "status": serialized.get("status") or quote_request.status,
            "status_label": serialized.get("status_label") or quote_request.status,
            "customer_name": quote_request.customer_name or "Client",
            "shop_name": getattr(quote_request.shop, "name", "") or "Awaiting production match",
            "assigned_manager": serialized.get("assigned_manager"),
            "assigned_manager_name": (serialized.get("assigned_manager") or {}).get("display_name") or "",
            "request_snapshot": serialized.get("request_snapshot") or {},
            "latest_response": serialized.get("latest_response"),
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
            "artwork_required": job.artwork_required,
            "artwork_uploaded": self._has_artwork(job),
            "payment_confirmed": str(job.payment_status or "").lower() in {"confirmed", "release_ready", "released"},
            "pricing": _job_pricing_snapshot(job, role),
        }
        if role == CANONICAL_CLIENT_ROLE:
            row.update(self._client_tracking_payload(job))
        if role == CANONICAL_PARTNER_ROLE:
            row["client_name"] = getattr(job.client, "name", "") or getattr(job.client, "email", "") or "Client"
            row["assigned_shop_name"] = getattr(job.assigned_shop, "name", "") or "Awaiting assignment"
        if role == CANONICAL_PRODUCTION_ROLE:
            row["assigned_shop_name"] = getattr(job.assigned_shop, "name", "") or "Production Shop"
            row["specs"] = self._production_specs_snapshot(job)
        return row


class ClientQuoteListView(BaseRoleDetailView):
    dashboard_role = "client"
    allowed_roles = (CANONICAL_CLIENT_ROLE,)

    def get(self, request):
        rows = (
            QuoteRequest.objects.filter(Q(created_by=request.user) | Q(on_behalf_of=request.user))
            .select_related("shop", "assigned_manager")
            .distinct()
            .order_by("-updated_at", "-created_at")
        )
        return Response({"role": "client", "results": [self._quote_row(item, request=request) for item in rows]})


class ClientQuoteDetailView(BaseRoleDetailView):
    dashboard_role = "client"
    allowed_roles = (CANONICAL_CLIENT_ROLE,)

    def get(self, request, pk):
        quote_request = get_object_or_404(
            QuoteRequest.objects.select_related("shop", "on_behalf_of", "assigned_manager"),
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
        return QuoteRequest.objects.filter(
            Q(created_by=request.user) | Q(assigned_manager=request.user) | Q(managed_jobs__broker=request.user)
        ).select_related("shop", "on_behalf_of", "assigned_manager").distinct().order_by("-updated_at", "-created_at")

    def get(self, request, pk=None):
        if pk is not None:
            quote_request = get_object_or_404(self.get_queryset(request), pk=pk)
            latest_response = quote_request.shop_quotes.exclude(status=ShopQuote.PENDING).select_related("shop").order_by("-created_at", "-id").first()
            payload = self._quote_row(quote_request, request=request)
            payload["client_name"] = quote_request.customer_name or getattr(quote_request.on_behalf_of, "name", "") or "Client"
            payload["client_email"] = quote_request.customer_email or getattr(quote_request.on_behalf_of, "email", "")
            payload["client_phone"] = quote_request.customer_phone
            payload["on_behalf_of_user_id"] = quote_request.on_behalf_of_id
            payload["latest_response"] = QuoteResponseReadSerializer(latest_response, context={"request": request}).data if latest_response else None
            managed_job = quote_request.managed_jobs.select_related("assigned_shop").order_by("-id").first()
            if managed_job:
                payload["managed_job"] = self._job_row(managed_job, role=CANONICAL_PARTNER_ROLE)
            return Response({"role": "partner", "quote": payload})
        return Response({"role": "partner", "results": [self._quote_row(item, request=request) for item in self.get_queryset(request)]})


class PartnerQuoteSendToClientView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get_queryset(self, request):
        return QuoteRequest.objects.filter(
            Q(created_by=request.user) | Q(assigned_manager=request.user) | Q(managed_jobs__broker=request.user)
        ).select_related("shop", "on_behalf_of", "assigned_manager").distinct()

    def post(self, request, pk):
        quote_request = get_object_or_404(self.get_queryset(request), pk=pk)
        request_snapshot = dict(quote_request.request_snapshot or {})
        pending_client = dict(request_snapshot.get("pending_client") or {})
        if quote_request.on_behalf_of_id is None and pending_client.get("client_user_id"):
            quote_request.on_behalf_of_id = pending_client["client_user_id"]
            quote_request.customer_name = pending_client.get("name") or quote_request.customer_name
            quote_request.customer_email = pending_client.get("email") or quote_request.customer_email
            quote_request.customer_phone = pending_client.get("phone") or quote_request.customer_phone
            quote_request.request_snapshot = request_snapshot
            quote_request.save(
                update_fields=["on_behalf_of", "customer_name", "customer_email", "customer_phone", "request_snapshot", "updated_at"]
            )
        if quote_request.on_behalf_of_id is None:
            return Response({"detail": "client_id is required for partner quote requests."}, status=400)
        latest_response = quote_request.shop_quotes.order_by("-created_at", "-id").first()
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
        if latest_response.status == ShopQuote.PENDING:
            latest_response = update_quote_response(
                response=latest_response,
                status=ShopQuote.SENT,
                response_snapshot=response_snapshot,
                total=base_price,
                note=str(request.data.get("note") or latest_response.note or "Partner quote prepared in Printy."),
            )
        else:
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


class PartnerQuoteAttachClientView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get_queryset(self, request):
        return QuoteRequest.objects.filter(
            Q(created_by=request.user) | Q(assigned_manager=request.user) | Q(managed_jobs__broker=request.user)
        ).select_related("shop", "on_behalf_of", "assigned_manager").distinct()

    @transaction.atomic
    def patch(self, request, pk):
        quote_request = get_object_or_404(self.get_queryset(request), pk=pk)
        if quote_request.status != "draft":
            return Response({"detail": "Only draft partner quotes can attach a client."}, status=400)
        serializer = PartnerQuoteAttachClientSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = _resolve_or_create_partner_client(
                partner_user=request.user,
                client_user=serializer.validated_data.get("client_user"),
                client_name=serializer.validated_data.get("client_name", ""),
                client_email=serializer.validated_data.get("client_email", ""),
                client_phone=serializer.validated_data.get("client_phone", ""),
                client_company=serializer.validated_data.get("client_company", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        request_snapshot = dict(quote_request.request_snapshot or {})
        request_snapshot["pending_client"] = {
            "client_user_id": payload["client_id"],
            "name": payload["name"],
            "email": payload["email"],
            "phone": payload["phone"],
            "company": payload["company"],
        }
        request_details = dict(request_snapshot.get("request_details") or {})
        request_details.update(
            {
                "customer_name": payload["name"],
                "customer_email": payload["email"],
                "customer_phone": payload["phone"],
                "client_company": payload["company"],
            }
        )
        request_snapshot["request_details"] = request_details
        quote_request.customer_name = payload["name"] or quote_request.customer_name
        quote_request.customer_email = payload["email"] or quote_request.customer_email
        quote_request.customer_phone = payload["phone"] or quote_request.customer_phone
        quote_request.request_snapshot = request_snapshot
        quote_request.save(update_fields=["customer_name", "customer_email", "customer_phone", "request_snapshot", "updated_at"])
        return Response(
            {
                "quote_request_id": quote_request.id,
                "client_id": payload["client_id"],
                "client_name": payload["name"],
                "client_email": payload["email"],
                "client_phone": payload["phone"],
                "client_company": payload["company"],
            }
        )


class PartnerAssignedRequestQuoteCreateView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get_queryset(self, request):
        return QuoteRequest.objects.filter(
            Q(created_by=request.user) | Q(assigned_manager=request.user) | Q(managed_jobs__broker=request.user)
        ).select_related("shop", "on_behalf_of", "assigned_manager").distinct()

    def post(self, request, pk):
        quote_request = get_object_or_404(self.get_queryset(request), pk=pk)
        if quote_request.assigned_manager_id != request.user.id:
            raise PermissionDenied("You cannot respond to this quote request.")
        serializer = PartnerQuotePreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = respond_to_assigned_quote_request(
                partner_user=request.user,
                quote_request=quote_request,
                shop=serializer.validated_data["shop"],
                pricing_snapshot=serializer.validated_data["pricing_snapshot"],
                partner_markup=serializer.validated_data["partner_markup"],
                note=str(request.data.get("note") or "").strip(),
            )
        except ValueError as exc:
            raise serializers.ValidationError({"detail": str(exc)}) from exc
        return Response(
            {
                "role": "partner",
                "quote_request_id": payload["quote_request"].id,
                "shop_quote": QuoteResponseReadSerializer(payload["shop_quote"], context={"request": request}).data,
                "partner_preview": payload["preview"],
            },
            status=201,
        )


class PartnerAssignedRequestShopOptionsView(BaseRoleDetailView):
    dashboard_role = "partner"
    allowed_roles = (CANONICAL_PARTNER_ROLE,)

    def get_queryset(self, request):
        return QuoteRequest.objects.filter(
            Q(created_by=request.user) | Q(assigned_manager=request.user) | Q(managed_jobs__broker=request.user)
        ).select_related("shop", "on_behalf_of", "assigned_manager").distinct()

    def post(self, request, pk):
        quote_request = get_object_or_404(self.get_queryset(request), pk=pk)
        if quote_request.assigned_manager_id != request.user.id:
            raise PermissionDenied("You cannot access production options for this quote request.")
        serializer = PartnerAssignedRequestShopOptionsSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        payload = build_partner_production_matches(
            self._assigned_request_match_payload(quote_request, serializer.validated_data)
        )
        return Response(PartnerProductionMatchResponseSerializer(payload).data)


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
        if not managed_job_has_artwork(managed_job=job):
            job.artwork_required = True
            job.save(update_fields=["artwork_required", "updated_at"])
            notify_missing_artwork(managed_job=job, actor=request.user, source="dispatch_attempt")
            return Response({"detail": "Artwork required before dispatch. Client has been notified."}, status=400)

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
        try:
            payload = _resolve_or_create_partner_client(
                partner_user=request.user,
                client_name=serializer.validated_data.get("name", ""),
                client_email=serializer.validated_data.get("email", ""),
                client_phone=serializer.validated_data.get("phone", ""),
                client_company=serializer.validated_data.get("company", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        return Response(
            {
                "client_id": payload["client_id"],
                "name": payload["name"],
                "phone": payload["phone"],
                "email": payload["email"],
                "company": payload["company"],
                "is_new": payload["is_new"],
            },
            status=201 if payload["is_new"] else 200,
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
        return (
            ManagedJob.objects.filter(_production_shop_filter(request.user))
            .select_related("assigned_shop", "source_quote_request")
            .distinct()
            .order_by("-operational_priority_level", "-updated_at")
        )

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
