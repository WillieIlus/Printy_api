import logging
from decimal import Decimal

from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User, UserProfile
from accounts.services.capabilities import has_capability
from accounts.services.roles import CANONICAL_PARTNER_ROLE, is_client, resolve_user_roles
from .visibility import CLIENT_ACTOR, TOPOLOGY_MANAGED, project_identity
from notifications.models import Notification
from notifications.services import notify_quote_event
from jobs.managed_services import create_assignment_for_managed_job, create_managed_job_from_accepted_quote
from jobs.models import ManagedJob
from quotes.choices import QuoteStatus, ShopQuoteStatus
from quotes.messaging import create_quote_message
from quotes.models import QuoteDraft, QuoteRequest, QuoteRequestMessage, ShopQuote
from quotes.partner_services import build_partner_quote_preview, create_partner_quote
from quotes.services_workflow import (
    create_quote_response,
    save_quote_draft,
    send_quote_draft_to_shops,
    update_quote_draft,
    update_quote_response,
)
from services.pricing.quote_builder import build_quote_preview
from services.pricing.booklet_builder import build_booklet_preview
from services.pricing.large_format_builder import build_large_format_preview
from services.pricing.calculator_config import get_calculator_config
from services.pricing.calculator_preview import build_public_calculator_preview
from services.pricing.urgency import apply_priority_pricing
from services.production_matching import build_partner_production_matches
from services.pricing.for_shops_wizard import (
    build_public_rate_wizard_config,
    build_public_rate_wizard_preview,
    build_rate_wizard_config,
    build_step_preview,
    complete_rate_wizard,
    save_step_values,
)
from services.pricing.mvp_rate_card import (
    build_public_rate_card_builder_config,
    build_shop_rate_card_setup,
    complete_shop_rate_card_setup,
    preview_public_rate_card_builder,
    save_shop_rate_card_setup,
)
from setup.services import SHOP_STATUS_ONLY_FIELDS, get_setup_status_for_shop, get_setup_status_for_user
from shops.models import Shop
from shops.services import can_manage_quotes, can_manage_shop
from inventory.models import Machine, Paper
from services.pricing.engine import calculate_sheet_pricing

from .throttling import GuestQuoteRequestThrottle
from .workflow_serializers import (
    BookletCalculatorPreviewSerializer,
    CalculatorConfigPreviewSerializer,
    CalculatorPreviewSerializer,
    ClientResponseListItemSerializer,
    ClientResponseRejectSerializer,
    ClientResponseReplySerializer,
    ClientQuoteRequestDetailSerializer,
    DashboardCalculatorPayloadSerializer,
    DashboardQuoteRequestSummarySerializer,
    IntakeRecommendedManagerQuerySerializer,
    IntakeSubmitSerializer,
    LargeFormatCalculatorPreviewSerializer,
    MvpRateCardPublicSaveSerializer,
    MvpRateCardPreviewSerializer,
    MvpRateCardSetupSerializer,
    PartnerQuoteCreateSerializer,
    PartnerProductionMatchResponseSerializer,
    PartnerQuotePreviewSerializer,
    QuoteDraftCreateSerializer,
    QuoteDraftReadSerializer,
    RecommendedPrintManagerSerializer,
    QuoteDraftSendSerializer,
    QuoteDraftUpdateSerializer,
    PublicRateWizardPreviewSerializer,
    RateWizardStepActionSerializer,
    QuoteRequestReadSerializer,
    QuoteResponseCreateSerializer,
    QuoteResponseReadSerializer,
    QuoteResponseUpdateSerializer,
    QuoteConversationMessageSerializer,
    ShopResponseReplySerializer,
)
from .public_matching_serializers import PublicCalculatorResponseSerializer

logger = logging.getLogger("api.workflow")
_MANAGER_ACTIVITY_LOOKBACK_DAYS = 30


def _partner_quote_client_error():
    return Response(
        {"detail": "client_id is required for partner quote requests."},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _safe_manager_profile(user: User) -> UserProfile | None:
    try:
        return user.profile
    except UserProfile.DoesNotExist:
        return None


def _safe_manager_display_name(user: User) -> str:
    return (getattr(user, "name", "") or "").strip() or "Print Manager"


def _manager_location_area(user: User) -> str:
    profile = _safe_manager_profile(user)
    if profile is None:
        return ""
    city = (getattr(profile, "city", "") or "").strip()
    state = (getattr(profile, "state", "") or "").strip()
    if city and state and city.lower() != state.lower():
        return f"{city}, {state}"
    return city or state


def _manager_bio(user: User) -> str:
    profile = _safe_manager_profile(user)
    if profile is None:
        return ""
    return (getattr(profile, "bio", "") or "").strip()


def _manager_brand_name(user: User) -> str:
    return _safe_manager_display_name(user)


def _product_type_from_snapshot(snapshot: dict | None) -> str:
    data = snapshot if isinstance(snapshot, dict) else {}
    nested = data.get("request_snapshot") if isinstance(data.get("request_snapshot"), dict) else {}
    calculator = data.get("calculator_inputs") if isinstance(data.get("calculator_inputs"), dict) else {}
    return str(
        nested.get("product_type")
        or data.get("product_type")
        or calculator.get("product_type")
        or ""
    ).strip().lower()


def _quoted_product_types_for_manager(user: User) -> set[str]:
    product_types: set[str] = set()
    quote_requests = QuoteRequest.objects.filter(
        Q(assigned_manager=user) | Q(managed_jobs__broker=user)
    ).distinct()
    for quote_request in quote_requests:
        product_type = _product_type_from_snapshot(getattr(quote_request, "request_snapshot", None))
        if product_type:
            product_types.add(product_type)
    return product_types


def _completed_product_types_for_manager(user: User) -> dict[str, int]:
    counts: dict[str, int] = {}
    jobs = ManagedJob.objects.filter(broker=user, status="completed").select_related("source_quote_request")
    for job in jobs:
        product_type = _product_type_from_snapshot(getattr(getattr(job, "source_quote_request", None), "request_snapshot", None))
        if not product_type:
            continue
        counts[product_type] = counts.get(product_type, 0) + 1
    return counts


def _manager_specializations(user: User) -> list[str]:
    product_counts = _completed_product_types_for_manager(user)
    ranked = sorted(product_counts.items(), key=lambda item: (-item[1], item[0]))
    return [product_type.replace("_", " ").title() for product_type, _count in ranked[:3]]


def _has_active_shop_relationship(user: User) -> bool:
    roles = set(resolve_user_roles(user))
    return "production" in roles


def _average_response_hours_for_manager(user: User) -> float | None:
    requests = (
        QuoteRequest.objects.filter(assigned_manager=user)
        .exclude(shop__isnull=False)
        .annotate(
            latest_response_created_at=Subquery(
                ShopQuote.objects.filter(quote_request=OuterRef("pk")).order_by("-created_at").values("created_at")[:1]
            ),
            latest_response_sent_at=Subquery(
                ShopQuote.objects.filter(quote_request=OuterRef("pk")).order_by("-created_at").values("sent_at")[:1]
            ),
        )
    )
    durations: list[float] = []
    for request_row in requests:
        response_at = getattr(request_row, "latest_response_sent_at", None) or getattr(request_row, "latest_response_created_at", None)
        created_at = getattr(request_row, "created_at", None)
        if not response_at or not created_at:
            continue
        durations.append(max((response_at - created_at).total_seconds(), 0) / 3600)
    if not durations:
        return None
    return round(sum(durations) / len(durations), 2)


def _response_speed_score(average_response_hours: float | None) -> Decimal:
    if average_response_hours is None:
        return Decimal("0.10")
    if average_response_hours < 2:
        return Decimal("1.0")
    if average_response_hours < 6:
        return Decimal("0.7")
    if average_response_hours < 24:
        return Decimal("0.4")
    return Decimal("0.1")


def _distance_km_for_manager(user: User, *, request) -> float | None:
    return None


def _proximity_score(distance_km: float | None) -> Decimal:
    if distance_km is None:
        return Decimal("0.0")
    if distance_km < 5:
        return Decimal("1.0")
    if distance_km < 15:
        return Decimal("0.6")
    if distance_km < 30:
        return Decimal("0.3")
    return Decimal("0.0")


def _satisfaction_rating_for_manager(user: User) -> float | None:
    return None


def _satisfaction_score(rating: float | None) -> Decimal:
    if rating is None:
        return Decimal("0.5")
    return Decimal(str(max(0.0, min(rating / 5.0, 1.0))))


def _manager_active_recently(user: User) -> bool:
    cutoff = timezone.now() - timezone.timedelta(days=_MANAGER_ACTIVITY_LOOKBACK_DAYS)
    if getattr(user, "last_login", None) and user.last_login >= cutoff:
        return True
    if ShopQuote.objects.filter(created_by=user).filter(Q(sent_at__gte=cutoff) | Q(created_at__gte=cutoff)).exists():
        return True
    return False


def _eligible_manager_candidate(user: User, *, current_user=None, product_type: str = "") -> bool:
    if not user or not getattr(user, "is_active", False):
        return False
    if current_user is not None and getattr(current_user, "id", None) == user.id:
        return False
    can_manage = bool(
        has_capability(user, "can_manage_clients")
        or has_capability(user, "can_source_jobs")
        or getattr(user, "partner_profile_enabled", False)
    )
    if not can_manage:
        return False
    product_types = _quoted_product_types_for_manager(user)
    return bool(
        _has_active_shop_relationship(user)
        or getattr(user, "partner_profile_enabled", False)
        or (product_type and product_type in product_types)
        or not product_type
    )


def _previous_manager_id_for_client(user) -> int | None:
    if not user or not getattr(user, "is_authenticated", False) or not is_client(user):
        return None
    return (
        ManagedJob.objects.filter(
            client=user,
            broker_id__isnull=False,
            status__in={
                "accepted",
                "payment_confirmed",
                "assigned",
                "in_production",
                "ready",
                "completed",
            },
        )
        .order_by("-completed_at", "-accepted_at", "-created_at")
        .values_list("broker_id", flat=True)
        .first()
    )


def _product_match_score(user: User, *, product_type: str) -> Decimal:
    completed_counts = _completed_product_types_for_manager(user)
    if completed_counts.get(product_type, 0) > 5:
        return Decimal("1.0")
    if product_type and product_type in _quoted_product_types_for_manager(user):
        return Decimal("0.6")
    return Decimal("0.3")


def _manager_completed_jobs(user: User) -> int:
    return ManagedJob.objects.filter(broker=user, status="completed").count()


def _is_previous_manager_eligible(user: User, *, client_user, product_type: str) -> bool:
    previous_manager_id = _previous_manager_id_for_client(client_user)
    if not previous_manager_id or user.id != previous_manager_id:
        return False
    return True


def _manager_badge(*, manager_payloads: list[dict[str, object]], index: int) -> str | None:
    payload = manager_payloads[index]
    if payload.get("is_previous_manager"):
        return None
    if index == 0:
        return "most_recommended"
    fastest_hours = min(
        [row["avg_response_hours"] for row in manager_payloads if row.get("avg_response_hours") is not None],
        default=None,
    )
    if fastest_hours is not None and payload.get("avg_response_hours") == fastest_hours:
        return "fast_responder"
    most_completed = max([int(row.get("completed_jobs") or 0) for row in manager_payloads], default=0)
    if most_completed and int(payload.get("completed_jobs") or 0) == most_completed:
        return "experienced"
    return None


def _build_recommended_manager_payloads(*, request, current_user, product_type: str, quantity: int, paper_gsm=None, size: str = "") -> dict[str, object]:
    candidates = (
        User.objects.filter(is_active=True)
        .select_related("profile")
        .prefetch_related("user_roles")
    )
    previous_manager_id = _previous_manager_id_for_client(current_user)
    eligible: list[User] = [
        candidate for candidate in candidates
        if _eligible_manager_candidate(candidate, current_user=current_user, product_type=product_type)
    ]

    scored: list[tuple[Decimal, User, dict[str, object]]] = []
    previous_payload: dict[str, object] | None = None
    previous_user: User | None = None
    for manager in eligible:
        avg_response_hours = _average_response_hours_for_manager(manager)
        completed_jobs = _manager_completed_jobs(manager)
        satisfaction_rating = _satisfaction_rating_for_manager(manager)
        distance_km = _distance_km_for_manager(manager, request=request)
        is_previous_manager = bool(previous_manager_id and manager.id == previous_manager_id)
        product_score = _product_match_score(manager, product_type=product_type)
        response_score = _response_speed_score(avg_response_hours)
        proximity_score = _proximity_score(distance_km)
        satisfaction_score = _satisfaction_score(satisfaction_rating)
        score = (
            product_score * Decimal("0.40")
            + response_score * Decimal("0.30")
            + proximity_score * Decimal("0.15")
            + satisfaction_score * Decimal("0.15")
        )
        payload = {
            "id": manager.id,
            "display_name": _safe_manager_display_name(manager),
            "brand_name": _manager_brand_name(manager),
            "specializations": _manager_specializations(manager),
            "avg_response_hours": avg_response_hours,
            "completed_jobs": completed_jobs,
            "satisfaction_rating": satisfaction_rating,
            "distance_km": distance_km,
            "is_previous_manager": is_previous_manager,
            "badge": None,
            "recommendation_reason": (
                "You have worked with this Print Manager before."
                if is_previous_manager
                else f"Completed {completed_jobs} managed jobs and can handle {product_type.replace('_', ' ')} requests."
                if completed_jobs
                else "Available to review your specs and prepare an exact quote."
            ),
        }
        if _is_previous_manager_eligible(manager, client_user=current_user, product_type=product_type):
            previous_payload = payload
            previous_user = manager
        scored.append((score, manager, payload))

    scored.sort(key=lambda item: (item[0], int(item[2].get("completed_jobs") or 0), bool(item[2].get("avg_response_hours") is not None)), reverse=True)
    selected_payloads: list[dict[str, object]] = []
    if previous_payload is not None:
        selected_payloads.append(previous_payload)
    for _score, manager, payload in scored:
        if previous_user is not None and manager.id == previous_user.id:
            continue
        selected_payloads.append(payload)
        if len(selected_payloads) >= 3:
            break
    selected_payloads = selected_payloads[:3]
    for index in range(len(selected_payloads)):
        selected_payloads[index]["badge"] = _manager_badge(manager_payloads=selected_payloads, index=index)
    return {
        "results": selected_payloads,
        "message": (
            ""
            if selected_payloads
            else "No managers available for this spec yet. Printy will handle your job directly."
        ),
        "meta": {
            "product_type": product_type,
            "quantity": quantity,
            "paper_gsm": paper_gsm,
            "size": size,
            "previous_manager_active": bool(previous_payload),
        },
    }


def _broadcast_group_requests(quote_request: QuoteRequest):
    if quote_request.source_draft_id:
        return quote_request.source_draft.generated_requests.select_related("shop")
    return QuoteRequest.objects.filter(pk=quote_request.pk).select_related("shop")


def _resolve_wizard_shop(*, request, shop_slug: str | None = None):
    slug = (shop_slug or "").strip()
    if slug:
        shop = get_object_or_404(Shop, slug=slug)
    else:
        shop = Shop.objects.filter(owner=request.user).order_by("id").first()
        if shop is None:
            return None
    if not can_manage_shop(shop, request.user):
        return None
    return shop


def _create_shop_from_rate_card_draft(*, user, shop_details: dict[str, str]):
    shop_name = (shop_details.get("shop_name") or "").strip() or "Print Shop"
    location_area = (shop_details.get("location_area") or "").strip() or "Nairobi"
    whatsapp = (shop_details.get("whatsapp_number") or "").strip()
    return Shop.objects.create(
        owner=user,
        name=shop_name,
        business_email=(getattr(user, "email", "") or "shop@printy.ke").strip() or "shop@printy.ke",
        public_email=(getattr(user, "email", "") or "").strip(),
        phone_number=whatsapp or "+254 700 000 000",
        public_whatsapp_number=whatsapp,
        address_line=location_area,
        city=location_area,
        state=location_area,
        country="Kenya",
        service_area=location_area,
        description="Created from Printy MVP rate card onboarding.",
        is_active=True,
        is_public=True,
    )


def _create_conversation_message(
    *,
    quote_request: QuoteRequest,
    shop_quote: ShopQuote,
    sender,
    recipient,
    sender_role: str,
    recipient_role: str,
    subject: str = "",
    message: str = "",
    conversation_type: str = "",
    proposed_price=None,
    proposed_turnaround: str = "",
    proposed_quantity=None,
    proposed_material: str = "",
    proposed_gsm: str = "",
    proposed_size: str = "",
    proposed_finishing=None,
):
    message_obj = create_quote_message(
        quote_request=quote_request,
        shop_quote=shop_quote,
        sender=sender,
        recipient=recipient,
        recipient_email=getattr(recipient, "email", "") if recipient else "",
        sender_role=sender_role,
        recipient_role=recipient_role,
        message_kind=QuoteRequestMessage.MessageKind.REPLY,
        message_type=QuoteRequestMessage.MessageType.QUOTE_CONVERSATION,
        direction=QuoteRequestMessage.Direction.INBOUND,
        subject=subject or "",
        body=message,
        metadata={},
        send_email_copy=bool(getattr(recipient, "email", "") if recipient else ""),
        create_failure_notice=True,
    )
    message_obj.conversation_type = conversation_type
    message_obj.proposed_price = proposed_price
    message_obj.proposed_turnaround = proposed_turnaround or ""
    message_obj.proposed_quantity = proposed_quantity
    message_obj.proposed_material = proposed_material or ""
    message_obj.proposed_gsm = proposed_gsm or ""
    message_obj.proposed_size = proposed_size or ""
    message_obj.proposed_finishing = proposed_finishing
    message_obj.save(
        update_fields=[
            "conversation_type",
            "proposed_price",
            "proposed_turnaround",
            "proposed_quantity",
            "proposed_material",
            "proposed_gsm",
            "proposed_size",
            "proposed_finishing",
            "updated_at",
        ]
    )
    return message_obj


class DashboardCalculatorPreviewView(APIView):
    """
    Shop-owner specific calculator preview.
    Uses the authenticated user's shop rate card and finishings.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Resolve shop
        shop = Shop.objects.filter(owner=request.user).order_by("id").first()
        if not shop:
            membership_shop = Shop.objects.filter(memberships__user=request.user, memberships__is_active=True).first()
            if membership_shop:
                shop = membership_shop.shop
        
        if not shop:
            return Response({"detail": "No active shop was found for your account. Please set up your shop first."}, status=status.HTTP_404_NOT_FOUND)
        
        serializer = DashboardCalculatorPayloadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        
        paper = validated["paper_id"]
        color_mode = validated.get("color_mode", "COLOR")
        sides = validated.get("sides", "SIMPLEX")
        
        # Ensure paper belongs to shop
        if paper.shop_id != shop.id:
            return Response({"detail": "Selected paper does not belong to your shop."}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve machine
        machine = Machine.objects.filter(
            shop=shop, 
            is_active=True,
            printing_rates__sheet_size=paper.sheet_size,
            printing_rates__color_mode=color_mode,
            printing_rates__is_active=True
        ).first()
        
        if not machine:
            machine = Machine.objects.filter(shop=shop, is_active=True, printing_rates__is_default=True).first()
            if machine and not machine.printing_rates.filter(sheet_size=paper.sheet_size, color_mode=color_mode, is_active=True).exists():
                machine = None
        
        if not machine:
            return Response({
                "detail": f"No active printing rate found for {paper.sheet_size} in {color_mode}. Please add this to your Pricing Setup first."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Ensure finishings belong to shop
        for fin in validated.get("finishings", []):
            if fin["rule"].shop_id != shop.id:
                return Response({"detail": f"Finishing '{fin['rule'].name}' does not belong to your shop."}, status=status.HTTP_400_BAD_REQUEST)

        # Calculate pricing
        try:
            result = calculate_sheet_pricing(
                shop=shop,
                quantity=validated["quantity"],
                paper=paper,
                machine=machine,
                color_mode=color_mode,
                sides=sides,
                finishing_selections=validated.get("finishings", []),
                width_mm=validated.get("width_mm"),
                height_mm=validated.get("height_mm"),
            )
            
            data = result.to_dict()
            data = apply_priority_pricing(
                data,
                urgency_type=validated.get("urgency_type"),
                requested_deadline=validated.get("requested_deadline"),
                requested_delivery_time=validated.get("requested_delivery_time"),
            )
            contract = data.get("calculation_result", {})
            
            response_data = {
                "can_calculate": result.can_calculate,
                "total": result.totals.get("grand_total"),
                "currency": result.currency,
                "price_mode": "exact", # Dashboard uses real rates
                "production_preview": {
                    "pieces_per_sheet": result.copies_per_sheet,
                    "sheets_required": result.good_sheets,
                    "parent_sheet": result.parent_sheet_name,
                    "quantity": result.quantity,
                    "cutting_required": result.breakdown.get("imposition", {}).get("cutting_required", True),
                    "warnings": contract.get("warnings", []),
                },
                "pricing_breakdown": {
                    "currency": result.currency,
                    "lines": contract.get("line_items", []),
                },
                "warnings": contract.get("warnings", []),
                "summary": f"Quote for {result.quantity} units on {paper.paper_type} {paper.gsm}gsm",
            }
            return Response(response_data)
        except Exception as e:
            logger.exception("Dashboard calculator error: %s", e)
            return Response({"detail": "We could not calculate this quote. Please check your rate card and try again."}, status=status.HTTP_400_BAD_REQUEST)


class SetupStatusCompatView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(get_setup_status_for_user(request.user))


class ShopSetupStatusCompatView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, shop_slug):
        shop = get_object_or_404(Shop.objects.only(*SHOP_STATUS_ONLY_FIELDS), slug=shop_slug)
        if not can_manage_shop(shop, request.user):
            return Response({"detail": "You cannot access this shop setup status."}, status=status.HTTP_403_FORBIDDEN)
        return Response(get_setup_status_for_shop(shop))


class CalculatorPreviewView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CalculatorPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        pricing = build_quote_preview(
            shop=validated["shop"],
            product=validated.get("product"),
            quantity=validated["quantity"],
            paper=validated["paper"],
            machine=validated["machine"],
            color_mode=validated["color_mode"],
            sides=validated["sides"],
            apply_duplex_surcharge=validated.get("apply_duplex_surcharge"),
            finishing_selections=validated.get("finishings") or [],
            width_mm=validated.get("width_mm"),
            height_mm=validated.get("height_mm"),
        )
        pricing = apply_priority_pricing(
            pricing,
            urgency_type=validated.get("urgency_type"),
            requested_deadline=validated.get("requested_deadline"),
            requested_delivery_time=validated.get("requested_delivery_time"),
        )
        return Response(pricing)


class CalculatorConfigView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        logger = logging.getLogger(__name__)
        try:
            return Response(get_calculator_config())
        except (OperationalError, ProgrammingError) as exc:
            logger.error("calculator/config DB error (run migrations): %s", exc)
            return Response(
                {"detail": "Calculator configuration unavailable. Pending database migration."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:
            logger.exception("calculator/config unexpected error: %s", exc)
            return Response(
                {"detail": "Calculator configuration unavailable."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ForShopsRateWizardConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        shop = _resolve_wizard_shop(request=request, shop_slug=request.query_params.get("shop_slug"))
        if shop is None:
            return Response({"detail": "No manageable shop was found for this user."}, status=status.HTTP_404_NOT_FOUND)
        return Response(build_rate_wizard_config(shop))


class ForShopsRateWizardPublicConfigView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(build_public_rate_wizard_config())


class ForShopsRateWizardPublicPreviewView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PublicRateWizardPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["preset_key"] != "business_cards":
            return Response({"detail": "Only business_cards is supported right now."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            build_public_rate_wizard_preview(
                quantity=serializer.validated_data["quantity"],
                rates=serializer.validated_data.get("rates") or {},
            )
        )


class ForShopsMvpRateCardPublicConfigView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(build_public_rate_card_builder_config())


class ForShopsMvpRateCardPublicPreviewView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = MvpRateCardPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            preview_public_rate_card_builder(
                paper_rows=serializer.validated_data.get("paper_rows") or [],
                finishing_rows=serializer.validated_data.get("finishing_rows") or [],
            )
        )


class ForShopsMvpRateCardSaveView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = MvpRateCardPublicSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not request.user.is_authenticated:
            return Response(
                {
                    "pending_auth": True,
                    "shop_details": serializer.validated_data.get("shop_details") or {},
                    "paper_rows": serializer.validated_data.get("paper_rows") or [],
                    "finishing_rows": serializer.validated_data.get("finishing_rows") or [],
                },
                status=status.HTTP_202_ACCEPTED,
            )

        shop = _resolve_wizard_shop(request=request)
        if shop is None:
            shop = _create_shop_from_rate_card_draft(
                user=request.user,
                shop_details=serializer.validated_data.get("shop_details") or {},
            )

        payload = save_shop_rate_card_setup(
            shop,
            paper_rows=serializer.validated_data.get("paper_rows") or [],
            finishing_rows=serializer.validated_data.get("finishing_rows") or [],
            shop_details=serializer.validated_data.get("shop_details") or {},
            completed=True,
        )
        setup_status = get_setup_status_for_shop(shop)
        return Response(
            {
                "saved": True,
                "shop_slug": shop.slug,
                "redirect_url": "/dashboard/shop/setup",
                "setup_status": setup_status,
                **payload,
            }
        )


class ShopMvpRateCardSetupView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        shop = _resolve_wizard_shop(request=request, shop_slug=request.query_params.get("shop_slug"))
        if shop is None:
            return Response({"detail": "No manageable shop was found for this user."}, status=status.HTTP_404_NOT_FOUND)
        return Response(build_shop_rate_card_setup(shop))

    def patch(self, request):
        serializer = MvpRateCardSetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        shop = _resolve_wizard_shop(request=request, shop_slug=request.data.get("shop_slug") or request.query_params.get("shop_slug"))
        if shop is None:
            return Response({"detail": "No manageable shop was found for this user."}, status=status.HTTP_404_NOT_FOUND)
        payload = save_shop_rate_card_setup(
            shop,
            paper_rows=serializer.validated_data.get("paper_rows") or [],
            finishing_rows=serializer.validated_data.get("finishing_rows") or [],
            shop_details=serializer.validated_data.get("shop_details") or {},
            completed=False,
        )
        return Response(payload)


class ShopMvpRateCardCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        shop = _resolve_wizard_shop(request=request, shop_slug=request.data.get("shop_slug"))
        if shop is None:
            return Response({"detail": "No manageable shop was found for this user."}, status=status.HTTP_404_NOT_FOUND)
        return Response(complete_shop_rate_card_setup(shop))


class ForShopsRateWizardPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RateWizardStepActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        shop = _resolve_wizard_shop(request=request, shop_slug=serializer.validated_data.get("shop_slug"))
        if shop is None:
            return Response({"detail": "No manageable shop was found for this user."}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            save_step_values(shop, serializer.validated_data["step_key"], serializer.validated_data.get("values") or [])
            preview = build_step_preview(
                shop,
                serializer.validated_data["step_key"],
                quantity=serializer.validated_data.get("quantity"),
            )
            transaction.set_rollback(True)
        return Response(preview)


class ForShopsRateWizardSaveStepView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RateWizardStepActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        shop = _resolve_wizard_shop(request=request, shop_slug=serializer.validated_data.get("shop_slug"))
        if shop is None:
            return Response({"detail": "No manageable shop was found for this user."}, status=status.HTTP_404_NOT_FOUND)
        return Response(save_step_values(shop, serializer.validated_data["step_key"], serializer.validated_data.get("values") or []))


class ForShopsRateWizardCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        shop = _resolve_wizard_shop(request=request, shop_slug=request.data.get("shop_slug"))
        if shop is None:
            return Response({"detail": "No manageable shop was found for this user."}, status=status.HTTP_404_NOT_FOUND)
        return Response(complete_rate_wizard(shop))


class CalculatorConfigPreviewView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        """
        Public homepage calculator preview.

        Example payload for DRF/curl:
        {
          "product_type": "business_card",
          "quantity": 100,
          "finished_size": "85x55mm",
          "print_sides": "DUPLEX",
          "color_mode": "COLOR",
          "requested_paper_category": "matt",
          "requested_gsm": 300,
          "lamination": "matt-lamination"
        }
        """
        serializer = CalculatorConfigPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        response = build_public_calculator_preview(serializer.validated_data)
        return Response(PublicCalculatorResponseSerializer(response).data)


class BookletCalculatorPreviewView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = BookletCalculatorPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        pricing = build_booklet_preview(
            shop=validated["shop"],
            quantity=validated["quantity"],
            width_mm=validated["width_mm"],
            height_mm=validated["height_mm"],
            total_pages=validated.get("total_pages"),
            binding_type=validated["binding_type"],
            cover_paper=validated.get("cover_paper"),
            insert_paper=validated.get("insert_paper"),
            cover_sides=validated["cover_sides"],
            insert_sides=validated["insert_sides"],
            cover_color_mode=validated["cover_color_mode"],
            insert_color_mode=validated["insert_color_mode"],
            cover_lamination_mode=validated["cover_lamination_mode"],
            cover_lamination_finishing_rate=validated.get("cover_lamination_finishing_rate"),
            finishing_selections=validated.get("finishings") or [],
            binding_finishing_rate=validated.get("binding_finishing_rate"),
            turnaround_hours=validated.get("turnaround_hours"),
        )
        pricing = apply_priority_pricing(
            pricing,
            urgency_type=validated.get("urgency_type"),
            turnaround_hours=validated.get("turnaround_hours"),
            requested_deadline=validated.get("requested_deadline"),
            requested_delivery_time=validated.get("requested_delivery_time"),
        )
        return Response(pricing)


class LargeFormatCalculatorPreviewView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LargeFormatCalculatorPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        pricing = build_large_format_preview(
            shop=validated["shop"],
            product_subtype=validated["product_subtype"],
            quantity=validated["quantity"],
            width_mm=validated["width_mm"],
            height_mm=validated["height_mm"],
            material=validated["material"],
            finishing_selections=validated.get("finishings") or [],
            hardware_finishing_rate=validated.get("hardware_finishing_rate"),
            turnaround_hours=validated.get("turnaround_hours"),
        )
        pricing = apply_priority_pricing(
            pricing,
            urgency_type=validated.get("urgency_type"),
            turnaround_hours=validated.get("turnaround_hours"),
            requested_deadline=validated.get("requested_deadline"),
            requested_delivery_time=validated.get("requested_delivery_time"),
        )
        return Response(pricing)


class QuoteDraftListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = QuoteDraftReadSerializer

    def get_queryset(self):
        return QuoteDraft.objects.filter(user=self.request.user).select_related("shop", "selected_product")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return QuoteDraftCreateSerializer
        return QuoteDraftReadSerializer

    def create(self, request, *args, **kwargs):
        if not is_client(request.user):
            return Response({"detail": "Only client accounts can save quote drafts."}, status=status.HTTP_403_FORBIDDEN)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        draft = save_quote_draft(
            user=request.user,
            selected_product=validated.get("selected_product"),
            shop=validated.get("shop"),
            title=validated.get("title", ""),
            calculator_inputs_snapshot=validated["calculator_inputs_snapshot"],
            pricing_snapshot=validated.get("pricing_snapshot"),
            custom_product_snapshot=validated.get("custom_product_snapshot"),
            request_details_snapshot=validated.get("request_details_snapshot"),
        )
        return Response(QuoteDraftReadSerializer(draft).data, status=status.HTTP_201_CREATED)


class QuoteDraftDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = QuoteDraftReadSerializer

    def get_queryset(self):
        return QuoteDraft.objects.filter(user=self.request.user)

    def patch(self, request, pk):
        draft = get_object_or_404(QuoteDraft, pk=pk, user=request.user)
        serializer = QuoteDraftUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            updated = update_quote_draft(draft=draft, **serializer.validated_data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(QuoteDraftReadSerializer(updated).data)


class QuoteDraftSendView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_client(request.user):
            return Response({"detail": "Only client accounts can send drafts to shops."}, status=status.HTTP_403_FORBIDDEN)
        draft = get_object_or_404(QuoteDraft, pk=pk, user=request.user)
        serializer = QuoteDraftSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request_details_snapshot = serializer.validated_data.get("request_details_snapshot") or {}
        if "selected_manager_id" in serializer.validated_data:
            request_details_snapshot["selected_manager_id"] = serializer.validated_data.get("selected_manager_id")
        if CANONICAL_PARTNER_ROLE in resolve_user_roles(request.user) and not request_details_snapshot.get("client_id"):
            return _partner_quote_client_error()
        try:
            quote_requests = send_quote_draft_to_shops(
                draft=draft,
                shops=list(serializer.validated_data.get("shops") or []),
                request_details_snapshot=request_details_snapshot,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(QuoteRequestReadSerializer(quote_requests, many=True).data, status=status.HTTP_201_CREATED)


class RecommendedPrintManagerListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = IntakeRecommendedManagerQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        payload = _build_recommended_manager_payloads(
            request=request,
            current_user=request.user,
            product_type=str(serializer.validated_data["product_type"]).strip().lower(),
            quantity=serializer.validated_data["quantity"],
            paper_gsm=serializer.validated_data.get("paper_gsm"),
            size=serializer.validated_data.get("size", ""),
        )
        payload["results"] = RecommendedPrintManagerSerializer(payload["results"], many=True).data
        return Response(payload)


class IntakeSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_client(request.user):
            return Response({"detail": "Only client accounts can submit intake requests."}, status=status.HTTP_403_FORBIDDEN)
        serializer = IntakeSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        draft_id = validated.get("draft_id")
        if draft_id:
            draft = get_object_or_404(QuoteDraft, pk=draft_id, user=request.user)
        else:
            draft = save_quote_draft(
                user=request.user,
                title=validated.get("title", ""),
                calculator_inputs_snapshot=validated["calculator_inputs_snapshot"],
                pricing_snapshot=validated.get("pricing_snapshot"),
                request_details_snapshot=validated.get("request_details_snapshot"),
            )

        request_details_snapshot = dict(validated.get("request_details_snapshot") or draft.request_details_snapshot or {})
        request_details_snapshot["selected_manager_id"] = validated.get("selected_manager_id")
        if validated.get("artwork_reference"):
            request_details_snapshot["artwork_reference"] = validated.get("artwork_reference")
        quote_requests = send_quote_draft_to_shops(
            draft=draft,
            shops=[],
            request_details_snapshot=request_details_snapshot,
        )
        quote_request = quote_requests[0]
        manager_name = (
            getattr(getattr(quote_request, "assigned_manager", None), "name", "")
            or getattr(getattr(quote_request, "assigned_manager", None), "email", "")
            or "Printy"
        )
        return Response(
            {
                "intake_id": quote_request.id,
                "manager_name": manager_name,
                "expected_response_by": None,
            },
            status=status.HTTP_201_CREATED,
        )


class PartnerQuotePreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not has_capability(request.user, "can_source_jobs"):
            return Response({"detail": "You cannot create partner quotes."}, status=status.HTTP_403_FORBIDDEN)
        serializer = PartnerQuotePreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = build_partner_quote_preview(
                pricing_snapshot=serializer.validated_data["pricing_snapshot"],
                shop=serializer.validated_data["shop"],
                partner_markup=serializer.validated_data["partner_markup"],
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class PartnerProductionMatchView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not (
            has_capability(request.user, "can_source_jobs")
            or getattr(request.user, "is_staff", False)
            or getattr(request.user, "is_superuser", False)
        ):
            return Response({"detail": "You cannot access production matches."}, status=status.HTTP_403_FORBIDDEN)
        serializer = CalculatorConfigPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = build_partner_production_matches(serializer.validated_data)
        return Response(PartnerProductionMatchResponseSerializer(payload).data)


class PartnerQuoteCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not has_capability(request.user, "can_source_jobs"):
            return Response({"detail": "You cannot create partner quotes."}, status=status.HTTP_403_FORBIDDEN)
        serializer = PartnerQuoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data.get("save_as_draft") and serializer.validated_data.get("client_user") is None:
            return _partner_quote_client_error()
        try:
            payload = create_partner_quote(
                partner_user=request.user,
                shop=serializer.validated_data["shop"],
                client_user=serializer.validated_data.get("client_user"),
                client_name=serializer.validated_data.get("client_name", ""),
                client_email=serializer.validated_data.get("client_email", ""),
                client_phone=serializer.validated_data.get("client_phone", ""),
                client_company=serializer.validated_data.get("client_company", ""),
                calculator_inputs_snapshot=serializer.validated_data["calculator_inputs_snapshot"],
                pricing_snapshot=serializer.validated_data["pricing_snapshot"],
                partner_markup=serializer.validated_data["partner_markup"],
                title=serializer.validated_data.get("title", ""),
                note=serializer.validated_data.get("note", ""),
                save_as_draft=serializer.validated_data.get("save_as_draft", False),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "quote_request_id": payload["quote_request"].id,
                "shop_quote": QuoteResponseReadSerializer(payload["shop_quote"], context={"request": request}).data,
                "partner_preview": payload["preview"],
                "status": payload["quote_request"].status,
            },
            status=status.HTTP_201_CREATED,
        )


class PartnerQuoteListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_capability(request.user, "can_source_jobs"):
            return Response({"detail": "You cannot access partner quotes."}, status=status.HTTP_403_FORBIDDEN)
        responses = ShopQuote.objects.filter(
            created_by=request.user,
            quote_request__request_snapshot__quote_source="partner_quote_builder",
        ).select_related("quote_request", "shop").order_by("-created_at")
        payload = []
        for response in responses:
            request_snapshot = getattr(response.quote_request, "request_snapshot", {}) or {}
            payload.append(
                {
                    "id": response.id,
                    "quote_request_id": response.quote_request_id,
                    "client_name": response.quote_request.customer_name,
                    "shop_name": response.shop.name if response.shop_id else "",
                    "status": response.status,
                    "total": str(response.total) if response.total is not None else None,
                    "partner_markup": request_snapshot.get("partner_markup"),
                    "partner_brand_name": request_snapshot.get("partner_brand_name"),
                    "share_token": response.share_links.first().token if response.share_links.exists() else None,
                    "created_at": response.created_at,
                }
            )
        return Response(payload)


class QuoteRequestListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        customer_requests = QuoteRequest.objects.filter(
            Q(created_by=request.user) | Q(assigned_manager=request.user)
        )
        managed_shop_ids = list(
            Shop.objects.filter(owner=request.user).values_list("id", flat=True)
        )
        if not managed_shop_ids:
            managed_shop_ids = list(
                Shop.objects.filter(memberships__user=request.user, memberships__is_active=True).values_list("id", flat=True)
            )
        shop_requests = QuoteRequest.objects.filter(shop_id__in=managed_shop_ids)
        combined = (customer_requests | shop_requests).distinct().select_related("shop", "source_draft").order_by("-created_at")
        return Response(QuoteRequestReadSerializer(combined, many=True).data)


class QuoteRequestDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        quote_request = get_object_or_404(
            QuoteRequest.objects.select_related("shop", "source_draft", "assigned_manager"),
            pk=pk,
        )
        is_owner = quote_request.created_by_id == request.user.id
        is_assigned_manager = quote_request.assigned_manager_id == request.user.id
        can_manage = bool(quote_request.shop_id and can_manage_quotes(quote_request.shop, request.user))
        if not is_owner and not is_assigned_manager and not can_manage:
            return Response({"detail": "You cannot access this quote request."}, status=status.HTTP_403_FORBIDDEN)
        return Response(QuoteRequestReadSerializer(quote_request).data)


class QuoteResponseListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, request_id):
        quote_request = get_object_or_404(QuoteRequest.objects.select_related("shop"), pk=request_id)
        is_owner = quote_request.created_by_id == request.user.id
        is_assigned_manager = quote_request.assigned_manager_id == request.user.id
        can_manage = bool(quote_request.shop_id and can_manage_quotes(quote_request.shop, request.user))
        if not is_owner and not is_assigned_manager and not can_manage:
            return Response({"detail": "You cannot access responses for this quote request."}, status=status.HTTP_403_FORBIDDEN)
        responses = quote_request.shop_quotes.order_by("-created_at")
        if (is_owner or is_assigned_manager) and not can_manage:
            responses = responses.exclude(status=ShopQuoteStatus.PENDING)
        return Response(QuoteResponseReadSerializer(responses, many=True, context={"request": request}).data)

    def post(self, request, request_id):
        quote_request = get_object_or_404(QuoteRequest.objects.select_related("shop"), pk=request_id)
        if not quote_request.shop_id or not can_manage_quotes(quote_request.shop, request.user):
            return Response({"detail": "You cannot respond to quote requests for this shop."}, status=status.HTTP_403_FORBIDDEN)
        serializer = QuoteResponseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        response = create_quote_response(
            quote_request=quote_request,
            shop=quote_request.shop,
            user=request.user,
            status=serializer.validated_data["status"],
            response_snapshot=serializer.validated_data["response_snapshot"],
            revised_pricing_snapshot=serializer.validated_data.get("revised_pricing_snapshot"),
            total=serializer.validated_data.get("total"),
            note=serializer.validated_data.get("note", ""),
            turnaround_days=serializer.validated_data.get("turnaround_days"),
        )
        if (
            response.status != ShopQuoteStatus.PENDING
            and quote_request.created_by_id
            and quote_request.created_by_id != request.user.id
        ):
            notify_quote_event(
                recipient=quote_request.created_by,
                notification_type=Notification.SHOP_QUOTE_SENT,
                message=f"{project_identity(quote_request.shop.name, actor=CLIENT_ACTOR)} sent a quote for request #{quote_request.id}.",
                object_type="quote_request",
                object_id=quote_request.id,
                actor=request.user,
            )
        return Response(QuoteResponseReadSerializer(response, context={"request": request}).data, status=status.HTTP_201_CREATED)


class QuoteResponseDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        response = get_object_or_404(ShopQuote.objects.select_related("quote_request", "shop"), pk=pk)
        is_owner = response.quote_request.created_by_id == request.user.id
        can_manage = can_manage_quotes(response.shop, request.user)
        if not is_owner and not can_manage:
            return Response({"detail": "You cannot access this quote response."}, status=status.HTTP_403_FORBIDDEN)
        return Response(QuoteResponseReadSerializer(response, context={"request": request}).data)

    def patch(self, request, pk):
        response = get_object_or_404(ShopQuote.objects.select_related("quote_request", "shop"), pk=pk)
        if not can_manage_quotes(response.shop, request.user):
            return Response({"detail": "You cannot update this quote response."}, status=status.HTTP_403_FORBIDDEN)
        serializer = QuoteResponseUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if "status" not in serializer.validated_data:
            return Response({"detail": "status is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            updated = update_quote_response(response=response, **serializer.validated_data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if (
            updated.status != ShopQuoteStatus.PENDING
            and updated.quote_request.created_by_id
            and updated.quote_request.created_by_id != request.user.id
        ):
            notify_quote_event(
                recipient=updated.quote_request.created_by,
                notification_type=Notification.SHOP_QUOTE_REVISED,
                message=f"{project_identity(updated.shop.name, actor=CLIENT_ACTOR)} revised the quote for request #{updated.quote_request.id}.",
                object_type="shop_quote",
                object_id=updated.id,
                actor=request.user,
            )
        return Response(QuoteResponseReadSerializer(updated, context={"request": request}).data)


class ShopHomeDashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, shop_slug=None):
        if shop_slug:
            shop = get_object_or_404(Shop, slug=shop_slug)
        else:
            shop = Shop.objects.filter(owner=request.user).order_by("id").first()
            if not shop:
                membership_shop = Shop.objects.filter(memberships__user=request.user, memberships__is_active=True).order_by("id").first()
                shop = membership_shop
        if not shop or not can_manage_quotes(shop, request.user):
            return Response({"detail": "No accessible shop dashboard."}, status=status.HTTP_403_FORBIDDEN)

        latest_response = ShopQuote.objects.filter(
            quote_request_id=OuterRef("pk")
        ).order_by("-created_at", "-id")
        received = QuoteRequest.objects.filter(shop=shop).select_related("source_draft").annotate(
            latest_response_id=Subquery(latest_response.values("id")[:1]),
            latest_response_reference=Subquery(latest_response.values("quote_reference")[:1]),
            latest_response_status=Subquery(latest_response.values("status")[:1]),
            latest_response_total=Subquery(latest_response.values("total")[:1]),
            latest_response_created_at=Subquery(latest_response.values("created_at")[:1]),
            latest_response_sent_at=Subquery(latest_response.values("sent_at")[:1]),
        )
        status_buckets = received.aggregate(
            pending=Count("id", filter=Q(latest_response_status__isnull=True) | Q(latest_response_status="pending")),
            modified=Count("id", filter=Q(latest_response_status="modified")),
            accepted=Count("id", filter=Q(latest_response_status="accepted")),
            rejected=Count("id", filter=Q(latest_response_status="rejected")),
        )
        responded_requests = received.exclude(latest_response_id__isnull=True)
        response_durations_hours = []
        for request_row in responded_requests:
            response_at = getattr(request_row, "latest_response_sent_at", None) or getattr(request_row, "latest_response_created_at", None)
            created_at = getattr(request_row, "created_at", None)
            if not response_at or not created_at:
                continue
            response_durations_hours.append(max((response_at - created_at).total_seconds(), 0) / 3600)

        average_response_hours = (
            round(sum(response_durations_hours) / len(response_durations_hours), 2)
            if response_durations_hours
            else None
        )
        stale_requests_count = received.filter(
            latest_response_id__isnull=True,
            created_at__lt=timezone.now() - timezone.timedelta(hours=24),
        ).count()

        return Response(
            {
                "shop": {"id": shop.id, "name": shop.name, "slug": shop.slug},
                "new_quote_requests": received.count(),
                "received_quote_requests": received.count(),
                "pending_responses_count": status_buckets["pending"],
                "responded_requests_count": responded_requests.count(),
                "accepted_quotes_count": status_buckets["accepted"],
                "average_response_hours": average_response_hours,
                "stale_requests_count": stale_requests_count,
                "status_counts": {
                    "pending": status_buckets["pending"],
                    "modified": status_buckets["modified"],
                    "responded": responded_requests.count(),
                    "accepted": status_buckets["accepted"],
                    "rejected": status_buckets["rejected"],
                },
                "recent_requests": DashboardQuoteRequestSummarySerializer(received.order_by("-created_at")[:10], many=True).data,
            }
        )


class GuestQuoteRequestView(APIView):
    """
    Create quote requests without authentication.
    Captures name + email/phone contact, sends to one or more shops.
    """

    permission_classes = [AllowAny]
    throttle_classes = [GuestQuoteRequestThrottle]

    def post(self, request):
        customer_email = (request.data.get("customer_email") or "").strip().lower()
        customer_phone = (request.data.get("customer_phone") or "").strip()
        customer_name = (request.data.get("customer_name") or "").strip()
        shop_ids = request.data.get("shop_ids") or []
        notes = (request.data.get("notes") or "").strip()
        request_details_snapshot = request.data.get("request_details_snapshot") or {}

        if not customer_email and not customer_phone:
            return Response(
                {"detail": "At least one contact is required: email or phone / WhatsApp."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not shop_ids or not isinstance(shop_ids, list):
            return Response(
                {"detail": "shop_ids must be a non-empty list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        shops = list(Shop.objects.filter(pk__in=shop_ids))
        if not shops:
            return Response({"detail": "No valid shops found."}, status=status.HTTP_400_BAD_REQUEST)

        display_name = customer_name or customer_email or customer_phone
        created_requests = []

        with transaction.atomic():
            for shop in shops:
                reference = f"QR-{timezone.now():%Y%m%d}"
                quote_request = QuoteRequest.objects.create(
                    shop=shop,
                    created_by=None,
                    customer_name=display_name,
                    customer_email=customer_email,
                    customer_phone=customer_phone,
                    notes=notes,
                    status=QuoteStatus.SUBMITTED,
                    request_snapshot={
                        "source": "guest_calculator_send",
                        "is_guest": True,
                        "request_details": request_details_snapshot,
                        "selected_shop": {"id": shop.id, "slug": shop.slug, "name": shop.name},
                        "buyer": {
                            "is_authenticated": False,
                            "name": customer_name,
                            "email": customer_email,
                            "phone": customer_phone,
                        },
                        "visibility": {
                            "actor": CLIENT_ACTOR,
                            "topology_mode": TOPOLOGY_MANAGED,
                            "exposes_internal_economics": False,
                        },
                    },
                )
                quote_request.request_reference = f"{reference}-{quote_request.id}"
                quote_request.save(update_fields=["request_reference", "updated_at"])
                create_quote_message(
                    quote_request=quote_request,
                    sender=None,
                    recipient=shop.owner,
                    recipient_email=getattr(shop.owner, "email", ""),
                    sender_role="client",
                    recipient_role="shop_owner",
                    message_kind="status",
                    message_type="quote_request_created",
                    direction="inbound",
                    subject=f"New quote request from {display_name or 'visitor'}",
                    body=notes or "A guest submitted a quote request in Printy.",
                    metadata={"status": QuoteStatus.SUBMITTED, "source": "guest_calculator_send"},
                    send_email_copy=bool(getattr(shop.owner, "email", "")),
                    create_failure_notice=True,
                )

                if shop.owner_id:
                    try:
                        notify_quote_event(
                            recipient=shop.owner,
                            notification_type=Notification.QUOTE_REQUEST_SUBMITTED,
                            message=f"New guest request from {display_name or 'a visitor'} — #{quote_request.id}.",
                            object_type="quote_request",
                            object_id=quote_request.id,
                            actor=None,
                        )
                    except Exception:
                        logger.exception("Failed to notify shop owner for guest request %s", quote_request.id)

                created_requests.append(quote_request)

        return Response(
            [
                {
                    "id": qr.id,
                    "request_reference": qr.request_reference,
                    "shop_name": qr.shop.name,
                    "shop_slug": qr.shop.slug,
                    "customer_name": qr.customer_name,
                    "customer_email": qr.customer_email,
                    "customer_phone": qr.customer_phone,
                    "status": qr.status,
                    "created_at": qr.created_at.isoformat(),
                }
                for qr in created_requests
            ],
            status=status.HTTP_201_CREATED,
        )


class ClientQuoteRequestDetailView(generics.RetrieveAPIView):
    """
    Client-specific detail view for a quote request.
    Returns full job details + all shop responses for comparison.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ClientQuoteRequestDetailSerializer

    def get_queryset(self):
        return QuoteRequest.objects.filter(
            Q(created_by=self.request.user) | Q(on_behalf_of=self.request.user)
        ).select_related(
            "shop", "source_draft", "delivery_location"
        ).prefetch_related(
            "items__product", "items__paper", "items__material", "items__finishings__finishing_rate",
            "attachments", "shop_quotes__shop", "shop_quotes__messages__sender"
        )


class ClientResponseListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        responses = ShopQuote.objects.filter(
            Q(quote_request__created_by=request.user) | Q(quote_request__on_behalf_of=request.user),
        ).exclude(
            status=ShopQuoteStatus.PENDING,
        ).select_related(
            "quote_request", "shop",
        ).prefetch_related(
            "messages",
        ).order_by("-updated_at", "-created_at")
        return Response(ClientResponseListItemSerializer(responses, many=True, context={"request": request}).data)


class ClientResponseAcceptView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, response_id):
        shop_quote = get_object_or_404(
            ShopQuote.objects.select_related("quote_request", "shop", "shop__owner").filter(
                Q(quote_request__created_by=request.user) | Q(quote_request__on_behalf_of=request.user)
            ),
            pk=response_id,
        )
        if shop_quote.status not in (ShopQuoteStatus.SENT, ShopQuoteStatus.REVISED, ShopQuoteStatus.MODIFIED):
            return Response({"detail": "Only sent or revised quotes can be accepted."}, status=status.HTTP_400_BAD_REQUEST)
        quote_request = shop_quote.quote_request
        if quote_request.status in (QuoteStatus.REJECTED, QuoteStatus.CANCELLED, QuoteStatus.EXPIRED, QuoteStatus.CLOSED):
            return Response({"detail": "This request can no longer be accepted."}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        with transaction.atomic():
            shop_quote.status = ShopQuoteStatus.ACCEPTED
            shop_quote.accepted_at = now
            shop_quote.rejected_at = None
            shop_quote.rejection_reason = ""
            shop_quote.rejection_message = ""
            shop_quote.save(
                update_fields=[
                    "status",
                    "accepted_at",
                    "rejected_at",
                    "rejection_reason",
                    "rejection_message",
                    "updated_at",
                ]
            )
            quote_request.status = QuoteStatus.CLOSED
            quote_request.save(update_fields=["status", "updated_at"])
            ShopQuote.objects.filter(
                quote_request=quote_request,
            ).exclude(
                pk=shop_quote.pk,
            ).exclude(
                status=ShopQuoteStatus.PENDING,
            ).update(
                status=ShopQuoteStatus.REJECTED,
                rejected_at=now,
                rejection_reason="Superseded by accepted quote",
                rejection_message="A newer quote for this request was accepted.",
                updated_at=now,
            )

            sibling_requests = list(_broadcast_group_requests(quote_request))
            sibling_request_ids = [item.id for item in sibling_requests if item.id != quote_request.id]
            if sibling_request_ids:
                QuoteRequest.objects.filter(pk__in=sibling_request_ids).update(status=QuoteStatus.CLOSED, updated_at=now)
                ShopQuote.objects.filter(
                    quote_request_id__in=sibling_request_ids,
                ).exclude(
                    pk=shop_quote.pk,
                ).exclude(
                    status=ShopQuoteStatus.PENDING,
                ).update(
                    status=ShopQuoteStatus.REJECTED,
                    rejected_at=now,
                    rejection_reason="Not selected",
                    rejection_message="Another quote was accepted.",
                    updated_at=now,
                )

            create_quote_message(
                quote_request=quote_request,
                shop_quote=shop_quote,
                sender=request.user,
                recipient=shop_quote.shop.owner,
                recipient_email=getattr(shop_quote.shop.owner, "email", ""),
                sender_role=QuoteRequestMessage.SenderRole.CLIENT,
                recipient_role=QuoteRequestMessage.RecipientRole.SHOP_OWNER,
                message_kind=QuoteRequestMessage.MessageKind.STATUS,
                message_type=QuoteRequestMessage.MessageType.QUOTE_ACCEPTED,
                direction=QuoteRequestMessage.Direction.INBOUND,
                subject=f"Quote accepted by {quote_request.customer_name or 'client'}",
                body="Your quote was accepted in Printy.",
                metadata={"quote_status": ShopQuoteStatus.ACCEPTED},
                send_email_copy=bool(getattr(shop_quote.shop.owner, "email", "")),
                create_failure_notice=True,
            )
            create_quote_message(
                quote_request=quote_request,
                shop_quote=shop_quote,
                sender=request.user,
                recipient=request.user,
                recipient_email=getattr(request.user, "email", ""),
                sender_role=QuoteRequestMessage.SenderRole.CLIENT,
                recipient_role=QuoteRequestMessage.RecipientRole.CLIENT,
                message_kind=QuoteRequestMessage.MessageKind.STATUS,
                message_type=QuoteRequestMessage.MessageType.QUOTE_ACCEPTED,
                direction=QuoteRequestMessage.Direction.OUTBOUND,
                subject="Accepted quote in Printy",
                body="You accepted this quote in Printy.",
                metadata={"quote_status": ShopQuoteStatus.ACCEPTED},
            )
            managed_job = create_managed_job_from_accepted_quote(
                quote_request=quote_request,
                shop_quote=shop_quote,
                accepted_by=request.user,
            )
        if shop_quote.shop.owner_id and shop_quote.shop.owner_id != request.user.id:
            notify_quote_event(
                recipient=shop_quote.shop.owner,
                notification_type=Notification.SHOP_QUOTE_ACCEPTED,
                message=f"Your quote for Request #{quote_request.id} was accepted.",
                object_type="shop_quote",
                object_id=shop_quote.id,
                actor=request.user,
            )

        return Response(
            {
                "id": shop_quote.id,
                "request_id": quote_request.id,
                "status": "accepted",
                "accepted_at": shop_quote.accepted_at,
                "message": "Quote accepted successfully.",
            }
        )


class ClientResponseRejectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, response_id):
        shop_quote = get_object_or_404(
            ShopQuote.objects.select_related("quote_request", "shop", "shop__owner").filter(
                Q(quote_request__created_by=request.user) | Q(quote_request__on_behalf_of=request.user)
            ),
            pk=response_id,
        )
        serializer = ClientResponseRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if shop_quote.status == ShopQuoteStatus.ACCEPTED:
            return Response({"detail": "Accepted quotes cannot be rejected."}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        shop_quote.status = ShopQuoteStatus.REJECTED
        shop_quote.rejected_at = now
        shop_quote.rejection_reason = serializer.validated_data["reason"]
        shop_quote.rejection_message = serializer.validated_data.get("message", "")
        shop_quote.save(
            update_fields=["status", "rejected_at", "rejection_reason", "rejection_message", "updated_at"]
        )
        create_quote_message(
            quote_request=shop_quote.quote_request,
            shop_quote=shop_quote,
            sender=request.user,
            recipient=shop_quote.shop.owner,
            recipient_email=getattr(shop_quote.shop.owner, "email", ""),
            sender_role=QuoteRequestMessage.SenderRole.CLIENT,
            recipient_role=QuoteRequestMessage.RecipientRole.SHOP_OWNER,
            message_kind=QuoteRequestMessage.MessageKind.REJECTION,
            message_type=QuoteRequestMessage.MessageType.QUOTE_REJECTED,
            direction=QuoteRequestMessage.Direction.INBOUND,
            subject="Quote rejected by client",
            body=shop_quote.rejection_message or shop_quote.rejection_reason,
            metadata={"reason": shop_quote.rejection_reason},
            send_email_copy=bool(getattr(shop_quote.shop.owner, "email", "")),
            create_failure_notice=True,
        )
        if shop_quote.shop.owner_id and shop_quote.shop.owner_id != request.user.id:
            notify_quote_event(
                recipient=shop_quote.shop.owner,
                notification_type=Notification.REQUEST_DECLINED,
                message=f"Client rejected your quote for Request #{shop_quote.quote_request_id}.",
                object_type="shop_quote",
                object_id=shop_quote.id,
                actor=request.user,
            )
        return Response(
            {
                "id": shop_quote.id,
                "request_id": shop_quote.quote_request_id,
                "status": "rejected",
                "rejected_at": shop_quote.rejected_at,
                "reason": shop_quote.rejection_reason,
            }
        )


class ClientResponseReplyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, response_id):
        shop_quote = get_object_or_404(
            ShopQuote.objects.select_related("quote_request", "shop", "shop__owner").filter(
                Q(quote_request__created_by=request.user) | Q(quote_request__on_behalf_of=request.user)
            ),
            pk=response_id,
        )
        serializer = ClientResponseReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message_obj = _create_conversation_message(
            quote_request=shop_quote.quote_request,
            shop_quote=shop_quote,
            sender=request.user,
            recipient=shop_quote.shop.owner,
            sender_role=QuoteRequestMessage.SenderRole.CLIENT,
            recipient_role=QuoteRequestMessage.RecipientRole.SHOP_OWNER,
            subject=serializer.validated_data.get("subject", ""),
            message=serializer.validated_data["message"],
            conversation_type=serializer.validated_data["message_type"],
            proposed_price=serializer.validated_data.get("proposed_price"),
            proposed_turnaround=serializer.validated_data.get("proposed_turnaround", ""),
            proposed_quantity=serializer.validated_data.get("proposed_quantity"),
            proposed_material=serializer.validated_data.get("proposed_material", ""),
            proposed_gsm=serializer.validated_data.get("proposed_gsm", ""),
            proposed_size=serializer.validated_data.get("proposed_size", ""),
            proposed_finishing=serializer.validated_data.get("proposed_finishing"),
        )
        if shop_quote.shop.owner_id and shop_quote.shop.owner_id != request.user.id:
            notify_quote_event(
                recipient=shop_quote.shop.owner,
                notification_type=Notification.BUYER_CLARIFICATION_SENT,
                message=f"{shop_quote.quote_request.customer_name or 'Client'} replied on Request #{shop_quote.quote_request_id}.",
                object_type="shop_quote",
                object_id=shop_quote.id,
                actor=request.user,
            )
        return Response(QuoteConversationMessageSerializer(message_obj).data, status=status.HTTP_201_CREATED)


class ShopResponseReplyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, response_id):
        shop_quote = get_object_or_404(
            ShopQuote.objects.select_related("quote_request", "shop", "quote_request__created_by"),
            pk=response_id,
        )
        if not can_manage_quotes(shop_quote.shop, request.user):
            return Response({"detail": "You cannot reply to this quote response."}, status=status.HTTP_403_FORBIDDEN)
        serializer = ShopResponseReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message_obj = _create_conversation_message(
            quote_request=shop_quote.quote_request,
            shop_quote=shop_quote,
            sender=request.user,
            recipient=shop_quote.quote_request.created_by,
            sender_role=QuoteRequestMessage.SenderRole.SHOP,
            recipient_role=QuoteRequestMessage.RecipientRole.CLIENT,
            subject=serializer.validated_data.get("subject", ""),
            message=serializer.validated_data["message"],
            conversation_type=QuoteRequestMessage.ConversationType.SHOP_REPLY,
            proposed_price=serializer.validated_data.get("proposed_price"),
            proposed_turnaround=serializer.validated_data.get("proposed_turnaround", ""),
            proposed_quantity=serializer.validated_data.get("proposed_quantity"),
            proposed_material=serializer.validated_data.get("proposed_material", ""),
            proposed_gsm=serializer.validated_data.get("proposed_gsm", ""),
            proposed_size=serializer.validated_data.get("proposed_size", ""),
            proposed_finishing=serializer.validated_data.get("proposed_finishing"),
        )
        if shop_quote.quote_request.created_by_id and shop_quote.quote_request.created_by_id != request.user.id:
            notify_quote_event(
                recipient=shop_quote.quote_request.created_by,
                notification_type=Notification.SHOP_QUESTION_ASKED,
                message=f"{project_identity(shop_quote.shop.name, actor=CLIENT_ACTOR)} replied to your quote follow-up for Request #{shop_quote.quote_request_id}.",
                object_type="shop_quote",
                object_id=shop_quote.id,
                actor=request.user,
            )
        return Response(QuoteConversationMessageSerializer(message_obj).data, status=status.HTTP_201_CREATED)


class ShopQuoteRequestDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        quote_request = get_object_or_404(
            QuoteRequest.objects.select_related("shop", "created_by", "delivery_location"),
            pk=pk,
        )
        if not can_manage_quotes(quote_request.shop, request.user):
            return Response({"detail": "You cannot access this quote request."}, status=status.HTTP_403_FORBIDDEN)
        response = quote_request.shop_quotes.exclude(status=ShopQuoteStatus.PENDING).order_by("-created_at", "-id").first()
        conversation = []
        if response:
            conversation = QuoteConversationMessageSerializer(
                response.messages.select_related("sender").order_by("created_at", "id"),
                many=True,
            ).data
        payload = QuoteRequestReadSerializer(quote_request).data
        payload["response"] = QuoteResponseReadSerializer(response, context={"request": request}).data if response else None
        payload["conversation"] = conversation
        return Response(payload)
