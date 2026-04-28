import logging

from django.db import OperationalError, ProgrammingError
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.services.roles import is_client
from notifications.models import Notification
from notifications.services import notify_quote_event
from quotes.choices import ShopQuoteStatus
from quotes.models import QuoteDraft, QuoteRequest, ShopQuote
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
from setup.services import get_setup_status_for_shop, get_setup_status_for_user
from shops.models import Shop
from shops.services import can_manage_quotes, can_manage_shop

from .workflow_serializers import (
    CalculatorPreviewSerializer,
    BookletCalculatorPreviewSerializer,
    CalculatorConfigPreviewSerializer,
    DashboardQuoteRequestSummarySerializer,
    LargeFormatCalculatorPreviewSerializer,
    QuoteDraftCreateSerializer,
    QuoteDraftReadSerializer,
    QuoteDraftSendSerializer,
    QuoteDraftUpdateSerializer,
    QuoteRequestReadSerializer,
    QuoteResponseCreateSerializer,
    QuoteResponseReadSerializer,
    QuoteResponseUpdateSerializer,
)
from .public_matching_serializers import PublicCalculatorResponseSerializer


class SetupStatusCompatView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(get_setup_status_for_user(request.user))


class ShopSetupStatusCompatView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, shop_slug):
        shop = get_object_or_404(Shop, slug=shop_slug)
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
        try:
            quote_requests = send_quote_draft_to_shops(
                draft=draft,
                shops=list(serializer.validated_data["shops"]),
                request_details_snapshot=serializer.validated_data.get("request_details_snapshot"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(QuoteRequestReadSerializer(quote_requests, many=True).data, status=status.HTTP_201_CREATED)


class QuoteRequestListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        customer_requests = QuoteRequest.objects.filter(created_by=request.user)
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
        quote_request = get_object_or_404(QuoteRequest.objects.select_related("shop", "source_draft"), pk=pk)
        is_owner = quote_request.created_by_id == request.user.id
        can_manage = can_manage_quotes(quote_request.shop, request.user)
        if not is_owner and not can_manage:
            return Response({"detail": "You cannot access this quote request."}, status=status.HTTP_403_FORBIDDEN)
        return Response(QuoteRequestReadSerializer(quote_request).data)


class QuoteResponseListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, request_id):
        quote_request = get_object_or_404(QuoteRequest.objects.select_related("shop"), pk=request_id)
        is_owner = quote_request.created_by_id == request.user.id
        can_manage = can_manage_quotes(quote_request.shop, request.user)
        if not is_owner and not can_manage:
            return Response({"detail": "You cannot access responses for this quote request."}, status=status.HTTP_403_FORBIDDEN)
        responses = quote_request.shop_quotes.order_by("-created_at")
        if is_owner and not can_manage:
            responses = responses.exclude(status=ShopQuoteStatus.PENDING)
        return Response(QuoteResponseReadSerializer(responses, many=True).data)

    def post(self, request, request_id):
        quote_request = get_object_or_404(QuoteRequest.objects.select_related("shop"), pk=request_id)
        if not can_manage_quotes(quote_request.shop, request.user):
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
                message=f"{quote_request.shop.name} sent a quote for request #{quote_request.id}.",
                object_type="quote_request",
                object_id=quote_request.id,
                actor=request.user,
            )
        return Response(QuoteResponseReadSerializer(response).data, status=status.HTTP_201_CREATED)


class QuoteResponseDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        response = get_object_or_404(ShopQuote.objects.select_related("quote_request", "shop"), pk=pk)
        is_owner = response.quote_request.created_by_id == request.user.id
        can_manage = can_manage_quotes(response.shop, request.user)
        if not is_owner and not can_manage:
            return Response({"detail": "You cannot access this quote response."}, status=status.HTTP_403_FORBIDDEN)
        return Response(QuoteResponseReadSerializer(response).data)

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
                message=f"{updated.shop.name} revised the quote for request #{updated.quote_request.id}.",
                object_type="shop_quote",
                object_id=updated.id,
                actor=request.user,
            )
        return Response(QuoteResponseReadSerializer(updated).data)


class ShopHomeDashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
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
