import logging

from django.db import OperationalError, ProgrammingError, transaction
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
from quotes.choices import QuoteStatus, ShopQuoteStatus
from quotes.messaging import create_quote_message
from quotes.models import QuoteDraft, QuoteRequest, QuoteRequestMessage, ShopQuote
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

from .throttling import GuestQuoteRequestThrottle
from .workflow_serializers import (
    BookletCalculatorPreviewSerializer,
    CalculatorConfigPreviewSerializer,
    CalculatorPreviewSerializer,
    ClientResponseListItemSerializer,
    ClientResponseRejectSerializer,
    ClientResponseReplySerializer,
    ClientQuoteRequestDetailSerializer,
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
    QuoteConversationMessageSerializer,
    ShopResponseReplySerializer,
)
from .public_matching_serializers import PublicCalculatorResponseSerializer

logger = logging.getLogger("api.workflow")


def _broadcast_group_requests(quote_request: QuoteRequest):
    if quote_request.source_draft_id:
        return quote_request.source_draft.generated_requests.select_related("shop")
    return QuoteRequest.objects.filter(pk=quote_request.pk).select_related("shop")


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
        return QuoteRequest.objects.filter(created_by=self.request.user).select_related(
            "shop", "source_draft", "delivery_location"
        ).prefetch_related(
            "items__product", "items__paper", "items__material", "items__finishings__finishing_rate",
            "attachments", "shop_quotes__shop", "shop_quotes__messages__sender"
        )


class ClientResponseListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        responses = ShopQuote.objects.filter(
            quote_request__created_by=request.user,
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
            ShopQuote.objects.select_related("quote_request", "shop", "shop__owner"),
            pk=response_id,
            quote_request__created_by=request.user,
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
                subject=f"Accepted quote from {shop_quote.shop.name}",
                body="You accepted this quote in Printy.",
                metadata={"quote_status": ShopQuoteStatus.ACCEPTED},
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
            ShopQuote.objects.select_related("quote_request", "shop", "shop__owner"),
            pk=response_id,
            quote_request__created_by=request.user,
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
            ShopQuote.objects.select_related("quote_request", "shop", "shop__owner"),
            pk=response_id,
            quote_request__created_by=request.user,
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
                message=f"{shop_quote.shop.name} replied to your quote follow-up for Request #{shop_quote.quote_request_id}.",
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
