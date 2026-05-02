"""
Quote marketplace API views — customer vs shop separation.

A. Customer: /quote-requests/ — create, list, retrieve, submit, accept, cancel
B. Shop: /shops/<slug>/incoming-requests/ — list, retrieve, send-quote, mark-viewed, decline
C. Shop: /sent-quotes/<id>/ — retrieve, partial_update (revise), create-job
"""
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from .permissions import IsQuoteRequestBuyer, IsQuoteRequestSeller, IsShopQuoteOwner
from rest_framework.response import Response

from notifications.models import Notification
from notifications.services import notify_quote_event
from quotes.choices import QuoteStatus, ShopQuoteStatus
from quotes.messaging import create_quote_message, mark_message_read, mark_messages_read
from quotes.models import QuoteRequest, QuoteRequestAttachment, QuoteRequestMessage, ShopQuote, ShopQuoteAttachment
from quotes.draft_pdf import render_quote_draft_pdf
from quotes.request_brief import build_quote_request_brief, build_quote_request_whatsapp_handoff
from shops.models import Shop

from .quote_serializers import (
    QuoteInboxMessageSerializer,
    QuoteRequestAttachmentSerializer,
    QuoteRequestAttachmentUploadSerializer,
    QuoteRequestCustomerCreateSerializer,
    QuoteRequestCustomerDetailSerializer,
    QuoteRequestCustomerListSerializer,
    QuoteRequestCustomerUpdateSerializer,
    QuoteRequestReplySerializer,
    QuoteRequestRejectSerializer,
    QuoteRequestShopDetailSerializer,
    QuoteRequestShopListSerializer,
    ShopQuoteAttachmentSerializer,
    ShopQuoteAttachmentUploadSerializer,
    ShopQuoteCreateSerializer,
    ShopQuoteDetailSerializer,
    ShopQuoteListSerializer,
    ShopQuoteUpdateSerializer,
)


def _create_request_message(*, quote_request, sender=None, sender_role="system", message_kind="note", body="", shop_quote=None, metadata=None):
    recipient = quote_request.created_by if sender_role == "shop" else quote_request.shop.owner
    recipient_role = (
        QuoteRequestMessage.RecipientRole.CLIENT
        if sender_role == "shop"
        else QuoteRequestMessage.RecipientRole.SHOP_OWNER
    )
    message_type = QuoteRequestMessage.MessageType.SYSTEM_NOTICE
    if sender_role == "client" and message_kind == "status" and not shop_quote:
        message_type = QuoteRequestMessage.MessageType.QUOTE_REQUEST_CREATED
    elif sender_role == "shop" and message_kind == "question":
        message_type = QuoteRequestMessage.MessageType.QUOTE_QUESTION
    elif sender_role == "client" and message_kind == "reply":
        message_type = QuoteRequestMessage.MessageType.QUOTE_QUESTION
    elif sender_role == "shop" and message_kind == "quote":
        message_type = QuoteRequestMessage.MessageType.QUOTE_RESPONSE_SENT
    elif sender_role == "shop" and message_kind == "rejection":
        message_type = QuoteRequestMessage.MessageType.QUOTE_REJECTED
    elif sender_role == "client" and message_kind == "status" and shop_quote:
        message_type = QuoteRequestMessage.MessageType.QUOTE_ACCEPTED
    return create_quote_message(
        quote_request=quote_request,
        sender=sender,
        recipient=recipient,
        recipient_email=getattr(recipient, "email", "") if recipient else "",
        sender_role=sender_role,
        recipient_role=recipient_role,
        message_kind=message_kind,
        message_type=message_type,
        direction=QuoteRequestMessage.Direction.INBOUND,
        body=body or "",
        shop_quote=shop_quote,
        metadata=metadata or {},
        send_email_copy=bool(getattr(recipient, "email", "") if recipient else ""),
        create_failure_notice=True,
    )


def _quote_request_brief_response(*, quote_request, viewer_role: str, include_buyer_contact: bool):
    return Response(
        build_quote_request_brief(
            quote_request,
            include_buyer_contact=include_buyer_contact,
            viewer_role=viewer_role,
        )
    )


def _quote_request_whatsapp_response(*, quote_request, viewer_role: str):
    return Response(build_quote_request_whatsapp_handoff(quote_request, viewer_role=viewer_role))


def _quote_request_pdf_response(*, quote_request):
    pdf_bytes = render_quote_draft_pdf(quote_request)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="quote-request-{quote_request.id}.pdf"'
    return response


# ---------------------------------------------------------------------------
# A. Customer: /quote-requests/
# ---------------------------------------------------------------------------


class CustomerQuoteRequestViewSet(viewsets.ModelViewSet):
    """
    Customer quote request flow.
    POST /quote-requests/ — create
    GET /quote-requests/ — list my requests
    GET /quote-requests/{id}/ — view one
    PATCH /quote-requests/{id}/ — update draft
    POST /quote-requests/{id}/submit/ — submit draft
    POST /quote-requests/{id}/accept/ — accept shop quote
    POST /quote-requests/{id}/cancel/ — cancel request
    """

    permission_classes = [IsAuthenticated, IsQuoteRequestBuyer]

    def get_queryset(self):
        user = self.request.user
        qs = QuoteRequest.objects.filter(created_by=user)
        if user.is_staff:
            qs = QuoteRequest.objects.all()
        return qs.select_related(
            "shop", "delivery_location"
        ).prefetch_related(
            "items__product", "items__paper", "items__material", "items__finishings__finishing_rate",
            "services__service_rate", "attachments", "messages__sender",
        ).order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "create":
            return QuoteRequestCustomerCreateSerializer
        if self.action in ("update", "partial_update"):
            return QuoteRequestCustomerUpdateSerializer
        if self.action == "list":
            return QuoteRequestCustomerListSerializer
        return QuoteRequestCustomerDetailSerializer

    def perform_create(self, serializer):
        serializer.save()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            QuoteRequestCustomerDetailSerializer(serializer.instance).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        qr = self.get_object()
        if qr.status != QuoteStatus.DRAFT:
            return Response(
                {"detail": "Only draft quote requests can be updated."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=["get"], url_path="brief")
    def brief(self, request, pk=None):
        qr = self.get_object()
        return _quote_request_brief_response(
            quote_request=qr,
            viewer_role="buyer",
            include_buyer_contact=True,
        )

    @action(detail=True, methods=["get"], url_path="whatsapp-handoff")
    def whatsapp_handoff(self, request, pk=None):
        qr = self.get_object()
        return _quote_request_whatsapp_response(quote_request=qr, viewer_role="buyer")

    @action(detail=True, methods=["get"], url_path="download-pdf")
    def download_pdf(self, request, pk=None):
        qr = self.get_object()
        return _quote_request_pdf_response(quote_request=qr)

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        """Submit draft (status -> submitted)."""

        qr = self.get_object()
        if qr.status != QuoteStatus.DRAFT:
            return Response(
                {"detail": "Only draft quote requests can be submitted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qr.status = QuoteStatus.SUBMITTED
        qr.save(update_fields=["status", "updated_at"])
        _create_request_message(
            quote_request=qr,
            sender=request.user,
            sender_role="client",
            message_kind="status",
            body="Request submitted to the shop.",
            metadata={"status": QuoteStatus.SUBMITTED},
        )
        create_quote_message(
            quote_request=qr,
            sender=request.user,
            recipient=request.user,
            sender_role=QuoteRequestMessage.SenderRole.CLIENT,
            recipient_role=QuoteRequestMessage.RecipientRole.CLIENT,
            message_kind=QuoteRequestMessage.MessageKind.STATUS,
            message_type=QuoteRequestMessage.MessageType.QUOTE_REQUEST_CREATED,
            direction=QuoteRequestMessage.Direction.OUTBOUND,
            subject=f"Quote request sent to {qr.shop.name}",
            body="Your quote request was sent successfully.",
            metadata={"status": QuoteStatus.SUBMITTED},
        )
        if qr.shop.owner_id and qr.shop.owner_id != request.user.id:
            notify_quote_event(
                recipient=qr.shop.owner,
                notification_type=Notification.QUOTE_REQUEST_SUBMITTED,
                message=f"New quote request #{qr.id} from {qr.customer_name or 'customer'}.",
                object_type="quote_request",
                object_id=qr.id,
                actor=request.user,
            )
        notify_quote_event(
            recipient=request.user,
            notification_type=Notification.QUOTE_REQUEST_SENT,
            message=f"Your quote request #{qr.id} was sent to {qr.shop.name}.",
            object_type="quote_request",
            object_id=qr.id,
            actor=request.user,
        )
        return Response(QuoteRequestCustomerDetailSerializer(qr).data)

    @action(detail=True, methods=["post"], url_path="accept")
    def accept(self, request, pk=None):
        """Accept a sent quote. Body: { "sent_quote_id": <id> } (or "shop_quote_id" for backwards compat)."""
        qr = self.get_object()
        shop_quote_id = request.data.get("sent_quote_id") or request.data.get("shop_quote_id")
        if not shop_quote_id:
            return Response(
                {"detail": "sent_quote_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        shop_quote = get_object_or_404(
            ShopQuote.objects.filter(quote_request=qr),
            pk=shop_quote_id,
        )
        if shop_quote.status not in (ShopQuoteStatus.SENT, ShopQuoteStatus.REVISED):
            return Response(
                {"detail": "Only sent or revised quotes can be accepted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        shop_quote.status = ShopQuoteStatus.ACCEPTED
        shop_quote.accepted_at = timezone.now()
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
        if qr.status in (QuoteStatus.REJECTED, QuoteStatus.CANCELLED, QuoteStatus.EXPIRED):
            return Response({"detail": "This request can no longer be accepted."}, status=status.HTTP_400_BAD_REQUEST)
        if qr.status != QuoteStatus.CLOSED:
            qr.status = QuoteStatus.CLOSED
            qr.save(update_fields=["status", "updated_at"])
        _create_request_message(
            quote_request=qr,
            sender=request.user,
            sender_role="client",
            message_kind="status",
            body="Client accepted the quote.",
            shop_quote=shop_quote,
            metadata={"quote_status": ShopQuoteStatus.ACCEPTED},
        )
        create_quote_message(
            quote_request=qr,
            sender=request.user,
            recipient=request.user,
            sender_role=QuoteRequestMessage.SenderRole.CLIENT,
            recipient_role=QuoteRequestMessage.RecipientRole.CLIENT,
            message_kind=QuoteRequestMessage.MessageKind.STATUS,
            message_type=QuoteRequestMessage.MessageType.QUOTE_ACCEPTED,
            direction=QuoteRequestMessage.Direction.OUTBOUND,
            subject=f"Accepted quote from {qr.shop.name}",
            body="You accepted this quote in Printy.",
            shop_quote=shop_quote,
            metadata={"quote_status": ShopQuoteStatus.ACCEPTED},
        )
        if qr.shop.owner_id and qr.shop.owner_id != request.user.id:
            notify_quote_event(
                recipient=qr.shop.owner,
                notification_type=Notification.SHOP_QUOTE_ACCEPTED,
                message=f"Your quote for request #{qr.id} was accepted.",
                object_type="shop_quote",
                object_id=shop_quote.id,
                actor=request.user,
            )
        return Response(QuoteRequestCustomerDetailSerializer(qr).data)

    @action(detail=True, methods=["post"], url_path="reply")
    def reply(self, request, pk=None):
        qr = self.get_object()
        serializer = QuoteRequestReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if qr.status != QuoteStatus.AWAITING_CLIENT_REPLY:
            return Response(
                {"detail": "This request is not waiting for a client reply."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        _create_request_message(
            quote_request=qr,
            sender=request.user,
            sender_role="client",
            message_kind="reply",
            body=serializer.validated_data["body"],
            metadata={"status": QuoteStatus.AWAITING_SHOP_ACTION},
        )
        qr.status = QuoteStatus.AWAITING_SHOP_ACTION
        qr.save(update_fields=["status", "updated_at"])
        if qr.shop.owner_id and qr.shop.owner_id != request.user.id:
            notify_quote_event(
                recipient=qr.shop.owner,
                notification_type=Notification.BUYER_CLARIFICATION_SENT,
                message=f"{qr.customer_name or 'Client'} replied to request #{qr.id}.",
                object_type="quote_request",
                object_id=qr.id,
                actor=request.user,
            )
        return Response(QuoteRequestCustomerDetailSerializer(qr).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """Cancel quote request (draft or submitted)."""
        qr = self.get_object()
        if qr.status in (QuoteStatus.CLOSED, QuoteStatus.CANCELLED):
            return Response(
                {"detail": "Cannot cancel a closed or already cancelled request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if qr.shop_quotes.filter(status=ShopQuoteStatus.ACCEPTED).exists():
            return Response(
                {"detail": "Cannot cancel a request after accepting a quote."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qr.status = QuoteStatus.CANCELLED
        qr.save(update_fields=["status", "updated_at"])
        _create_request_message(
            quote_request=qr,
            sender=request.user,
            sender_role="client",
            message_kind="status",
            body="Request cancelled by the client.",
            metadata={"status": QuoteStatus.CANCELLED},
        )
        if qr.shop.owner_id and qr.shop.owner_id != request.user.id:
            notify_quote_event(
                recipient=qr.shop.owner,
                notification_type=Notification.QUOTE_REQUEST_CANCELLED,
                message=f"Quote request #{qr.id} was cancelled.",
                object_type="quote_request",
                object_id=qr.id,
                actor=request.user,
            )
        return Response(QuoteRequestCustomerDetailSerializer(qr).data)


# ---------------------------------------------------------------------------
# B. Shop: /shops/<slug>/incoming-requests/
# ---------------------------------------------------------------------------


class IncomingRequestViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Shop incoming quote requests.
    GET /shops/<slug>/incoming-requests/ — list
    GET /shops/<slug>/incoming-requests/{id}/ — view detail
    POST /shops/<slug>/incoming-requests/{id}/accept-request/ — accept and begin work
    POST /shops/<slug>/incoming-requests/{id}/ask-question/ — request clarification
    POST /shops/<slug>/incoming-requests/{id}/reject-request/ — reject request with reason
    POST /shops/<slug>/incoming-requests/{id}/send-quote/ — send shop quote
    """

    permission_classes = [IsAuthenticated, IsQuoteRequestSeller]
    lookup_url_kwarg = "request_id"

    def get_shop(self):
        return get_object_or_404(Shop, slug=self.kwargs["shop_slug"], is_active=True)

    def get_queryset(self):
        shop = self.get_shop()
        if not self.check_shop_owner():
            return QuoteRequest.objects.none()
        return QuoteRequest.objects.filter(shop=shop).select_related(
            "shop", "delivery_location"
        ).prefetch_related(
            "items__product", "items__paper", "items__material", "items__finishings__finishing_rate",
            "services__service_rate", "shop_quotes", "attachments", "messages__sender",
        ).order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "list":
            return QuoteRequestShopListSerializer
        return QuoteRequestShopDetailSerializer

    def check_shop_owner(self):
        shop = self.get_shop()
        if self.request.user.is_staff:
            return True
        if shop.owner_id == self.request.user.id:
            return True
        return shop.memberships.filter(user=self.request.user, is_active=True).exists()

    def list(self, request, *args, **kwargs):
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        return super().retrieve(request, *args, **kwargs)

    @action(detail=True, methods=["get"], url_path="brief")
    def brief(self, request, shop_slug=None, request_id=None):
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        qr = self.get_object()
        return _quote_request_brief_response(
            quote_request=qr,
            viewer_role="shop",
            include_buyer_contact=True,
        )

    @action(detail=True, methods=["get"], url_path="whatsapp-handoff")
    def whatsapp_handoff(self, request, shop_slug=None, request_id=None):
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        qr = self.get_object()
        return _quote_request_whatsapp_response(quote_request=qr, viewer_role="shop")

    @action(detail=True, methods=["get"], url_path="download-pdf")
    def download_pdf(self, request, shop_slug=None, request_id=None):
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        qr = self.get_object()
        return _quote_request_pdf_response(quote_request=qr)

    @action(detail=True, methods=["post"], url_path="accept-request")
    def accept_request(self, request, shop_slug=None, request_id=None):
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        qr = self.get_object()
        if qr.status in (QuoteStatus.QUOTED, QuoteStatus.REJECTED, QuoteStatus.CANCELLED, QuoteStatus.EXPIRED):
            return Response({"detail": "This request cannot be accepted in its current state."}, status=status.HTTP_400_BAD_REQUEST)
        qr.status = QuoteStatus.ACCEPTED
        qr.save(update_fields=["status", "updated_at"])
        _create_request_message(
            quote_request=qr,
            sender=request.user,
            sender_role="shop",
            message_kind="status",
            body="The shop accepted this request and is preparing a quote.",
            metadata={"status": QuoteStatus.ACCEPTED},
        )
        return Response(QuoteRequestShopDetailSerializer(qr).data)

    @action(detail=True, methods=["post"], url_path="ask-question")
    def ask_question(self, request, shop_slug=None, request_id=None):
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        qr = self.get_object()
        serializer = QuoteRequestReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if qr.status in (QuoteStatus.QUOTED, QuoteStatus.REJECTED, QuoteStatus.CANCELLED, QuoteStatus.EXPIRED):
            return Response({"detail": "This request can no longer receive clarification messages."}, status=status.HTTP_400_BAD_REQUEST)
        qr.status = QuoteStatus.AWAITING_CLIENT_REPLY
        qr.save(update_fields=["status", "updated_at"])
        _create_request_message(
            quote_request=qr,
            sender=request.user,
            sender_role="shop",
            message_kind="question",
            body=serializer.validated_data["body"],
            metadata={"status": QuoteStatus.AWAITING_CLIENT_REPLY},
        )
        if qr.created_by_id and qr.created_by_id != request.user.id:
            notify_quote_event(
                recipient=qr.created_by,
                notification_type=Notification.SHOP_QUESTION_ASKED,
                message=f"{qr.shop.name} asked a question about request #{qr.id}.",
                object_type="quote_request",
                object_id=qr.id,
                actor=request.user,
            )
        return Response(QuoteRequestShopDetailSerializer(qr).data)

    @action(detail=True, methods=["post"], url_path="reject-request")
    def reject_request(self, request, shop_slug=None, request_id=None):
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        qr = self.get_object()
        serializer = QuoteRequestRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if qr.status in (QuoteStatus.QUOTED, QuoteStatus.REJECTED, QuoteStatus.CANCELLED, QuoteStatus.EXPIRED):
            return Response({"detail": "This request cannot be rejected in its current state."}, status=status.HTTP_400_BAD_REQUEST)
        qr.status = QuoteStatus.REJECTED
        qr.save(update_fields=["status", "updated_at"])
        _create_request_message(
            quote_request=qr,
            sender=request.user,
            sender_role="shop",
            message_kind="rejection",
            body=serializer.validated_data["reason"],
            metadata={"status": QuoteStatus.REJECTED},
        )
        if qr.created_by_id and qr.created_by_id != request.user.id:
            notify_quote_event(
                recipient=qr.created_by,
                notification_type=Notification.REQUEST_DECLINED,
                message=f"{qr.shop.name} declined request #{qr.id}.",
                object_type="quote_request",
                object_id=qr.id,
                actor=request.user,
            )
        return Response(QuoteRequestShopDetailSerializer(qr).data)

    @action(detail=True, methods=["post"], url_path="send-quote")
    def send_quote(self, request, shop_slug=None, request_id=None):
        """Send shop quote. Body: { "total", "note", "turnaround_days" }."""
        from django.db import transaction
        from django.utils import timezone
        from quotes.pricing_service import compute_and_store_pricing

        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        qr = self.get_object()
        if qr.status not in (
            QuoteStatus.SUBMITTED,
            QuoteStatus.VIEWED,
            QuoteStatus.ACCEPTED,
            QuoteStatus.AWAITING_SHOP_ACTION,
        ):
            return Response(
                {"detail": "This request is not ready for a quote yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = ShopQuoteCreateSerializer(
            data=request.data,
            context={"quote_request": qr, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            for item in qr.items.all():
                compute_and_store_pricing(item)
            total = serializer.validated_data.get("total")
            if total is None:
                total = sum((i.line_total or 0) for i in qr.items.all())
            quote_status = serializer.validated_data.get("status", ShopQuoteStatus.SENT)
            now = timezone.now()
            pending_quote = qr.shop_quotes.filter(status=ShopQuoteStatus.PENDING).order_by("-created_at").first()
            if pending_quote:
                update_serializer = ShopQuoteUpdateSerializer(
                    pending_quote,
                    data={**request.data, "total": total},
                    partial=True,
                )
                update_serializer.is_valid(raise_exception=True)
                shop_quote = update_serializer.save(status=quote_status)
            else:
                shop_quote = serializer.save()
            from quotes.summary_service import get_shop_quote_summary_text

            shop_quote.total = total
            if quote_status != ShopQuoteStatus.PENDING:
                shop_quote.sent_at = now
                shop_quote.pricing_locked_at = now
            shop_quote.whatsapp_message = get_shop_quote_summary_text(shop_quote)
            update_fields = ["total", "whatsapp_message", "updated_at"]
            if quote_status != ShopQuoteStatus.PENDING:
                update_fields.extend(["sent_at", "pricing_locked_at"])
            shop_quote.save(update_fields=update_fields)
            if quote_status != ShopQuoteStatus.PENDING:
                qr.items.update(shop_quote=shop_quote)
                qr.status = QuoteStatus.QUOTED
                qr.save(update_fields=["status", "updated_at"])
                _create_request_message(
                    quote_request=qr,
                    sender=request.user,
                    sender_role="shop",
                    message_kind="quote",
                    body=serializer.validated_data.get("note", "") or "The shop sent a quote.",
                    shop_quote=shop_quote,
                    metadata={
                        "status": QuoteStatus.QUOTED,
                        "quote_status": shop_quote.status,
                        "total": str(total),
                        "turnaround_days": shop_quote.turnaround_days,
                        "turnaround_hours": shop_quote.turnaround_hours,
                        "estimated_ready_at": shop_quote.estimated_ready_at.isoformat() if shop_quote.estimated_ready_at else None,
                        "human_ready_text": shop_quote.human_ready_text,
                    },
                )
                create_quote_message(
                    quote_request=qr,
                    shop_quote=shop_quote,
                    sender=request.user,
                    recipient=request.user,
                    sender_role=QuoteRequestMessage.SenderRole.SHOP,
                    recipient_role=QuoteRequestMessage.RecipientRole.SHOP_OWNER,
                    message_kind=QuoteRequestMessage.MessageKind.QUOTE,
                    message_type=QuoteRequestMessage.MessageType.QUOTE_RESPONSE_SENT,
                    direction=QuoteRequestMessage.Direction.OUTBOUND,
                    subject=f"Quote sent to {qr.customer_name or 'client'}",
                    body=serializer.validated_data.get("note", "") or "The shop sent a quote.",
                    metadata={
                        "status": QuoteStatus.QUOTED,
                        "quote_status": shop_quote.status,
                        "total": str(total),
                        "turnaround_days": shop_quote.turnaround_days,
                    },
                )
            else:
                qr.status = QuoteStatus.AWAITING_SHOP_ACTION
                qr.save(update_fields=["status", "updated_at"])
        if quote_status != ShopQuoteStatus.PENDING and qr.created_by_id and qr.created_by_id != request.user.id:
            notify_quote_event(
                recipient=qr.created_by,
                notification_type=Notification.SHOP_QUOTE_SENT,
                message=f"{qr.shop.name} sent a quote for request #{qr.id}.",
                object_type="quote_request",
                object_id=qr.id,
                actor=request.user,
            )
        return Response(
            QuoteRequestShopDetailSerializer(qr).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="mark-viewed")
    def mark_viewed(self, request, shop_slug=None, request_id=None):
        """Mark request as viewed by shop."""
        if not self.check_shop_owner():
            return Response({"detail": "Not your shop."}, status=status.HTTP_403_FORBIDDEN)
        qr = self.get_object()
        if qr.status == QuoteStatus.SUBMITTED:
            qr.status = QuoteStatus.VIEWED
            qr.save(update_fields=["status", "updated_at"])
        return Response(QuoteRequestShopDetailSerializer(qr).data)

    @action(detail=True, methods=["post"], url_path="decline")
    def decline(self, request, shop_slug=None, request_id=None):
        """Backward-compatible alias for reject-request."""
        return self.reject_request(request, shop_slug=shop_slug, request_id=request_id)


# ---------------------------------------------------------------------------
# C. Shop: /sent-quotes/
# ---------------------------------------------------------------------------


class ShopQuoteViewSet(viewsets.ModelViewSet):
    """
    Sent quotes — shop's quotes sent to customers.
    GET /sent-quotes/ — list shop's sent quotes
    GET /sent-quotes/{id}/ — view detail
    PATCH /sent-quotes/{id}/ — revise (note, turnaround_days, total)
    POST /sent-quotes/{id}/create-job/ — create job from accepted quote
    """

    permission_classes = [IsAuthenticated, IsShopQuoteOwner]
    http_method_names = ["get", "head", "options", "patch"]

    def get_queryset(self):
        user = self.request.user
        qs = ShopQuote.objects.filter(shop__owner=user)
        if user.is_staff:
            qs = ShopQuote.objects.all()
        return qs.select_related(
            "quote_request", "shop"
        ).prefetch_related(
            "items__product", "items__paper", "items__material", "items__finishings__finishing_rate",
            "attachments",
        ).order_by("-sent_at", "-created_at")

    def get_serializer_class(self):
        if self.action in ("update", "partial_update"):
            return ShopQuoteUpdateSerializer
        if self.action == "list":
            return ShopQuoteListSerializer
        return ShopQuoteDetailSerializer

    def partial_update(self, request, *args, **kwargs):
        quote = self.get_object()
        if quote.shop.owner_id != request.user.id:
            return Response({"detail": "Not your shop quote."}, status=status.HTTP_403_FORBIDDEN)
        response = super().partial_update(request, *args, **kwargs)
        quote.refresh_from_db()
        requested_status = request.data.get("status")
        is_pending = quote.status == ShopQuoteStatus.PENDING or requested_status == ShopQuoteStatus.PENDING
        if is_pending:
            quote.quote_request.status = QuoteStatus.AWAITING_SHOP_ACTION
            quote.quote_request.save(update_fields=["status", "updated_at"])
            return response
        if quote.status != ShopQuoteStatus.REVISED:
            quote.status = ShopQuoteStatus.REVISED
        from quotes.summary_service import get_shop_quote_summary_text

        quote.whatsapp_message = get_shop_quote_summary_text(quote)
        quote.save(update_fields=["status", "whatsapp_message", "updated_at"])
        _create_request_message(
            quote_request=quote.quote_request,
            sender=request.user,
            sender_role="shop",
            message_kind="quote",
            body=quote.note or "The shop revised the quote.",
            shop_quote=quote,
            metadata={
                "status": QuoteStatus.QUOTED,
                "quote_status": quote.status,
                "total": str(quote.total or ""),
                "turnaround_days": quote.turnaround_days,
                "turnaround_hours": quote.turnaround_hours,
                "estimated_ready_at": quote.estimated_ready_at.isoformat() if quote.estimated_ready_at else None,
                "human_ready_text": quote.human_ready_text,
            },
        )
        create_quote_message(
            quote_request=quote.quote_request,
            shop_quote=quote,
            sender=request.user,
            recipient=request.user,
            sender_role=QuoteRequestMessage.SenderRole.SHOP,
            recipient_role=QuoteRequestMessage.RecipientRole.SHOP_OWNER,
            message_kind=QuoteRequestMessage.MessageKind.QUOTE,
            message_type=QuoteRequestMessage.MessageType.QUOTE_RESPONSE_SENT,
            direction=QuoteRequestMessage.Direction.OUTBOUND,
            subject=f"Quote sent to {quote.quote_request.customer_name or 'client'}",
            body=quote.note or "The shop revised the quote.",
            metadata={
                "status": QuoteStatus.QUOTED,
                "quote_status": quote.status,
                "total": str(quote.total or ""),
                "turnaround_days": quote.turnaround_days,
            },
        )
        qr = quote.quote_request
        qr.status = QuoteStatus.QUOTED
        qr.save(update_fields=["status", "updated_at"])
        if qr.created_by_id and qr.created_by_id != request.user.id:
            notify_quote_event(
                recipient=qr.created_by,
                notification_type=Notification.SHOP_QUOTE_REVISED,
                message=f"{quote.shop.name} revised the quote for request #{qr.id}.",
                object_type="shop_quote",
                object_id=quote.id,
                actor=request.user,
            )
        return response

    @action(detail=True, methods=["post"], url_path="create-job")
    def create_job(self, request, pk=None):
        """Create production job from accepted shop quote."""
        from production.serializers import ProductionOrderSerializer, ProductionOrderWriteSerializer

        quote = self.get_object()
        if quote.shop.owner_id != request.user.id:
            return Response({"detail": "Not your shop quote."}, status=status.HTTP_403_FORBIDDEN)
        if quote.status != ShopQuoteStatus.ACCEPTED:
            return Response(
                {"detail": "Only accepted quotes can be turned into jobs."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if quote.production_orders.exists():
            return Response(
                {"detail": "Job already created from this quote."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = request.data.copy() if request.data else {}
        data["shop_quote"] = quote.id
        serializer = ProductionOrderWriteSerializer(
            data=data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        job = serializer.save()
        return Response(
            ProductionOrderSerializer(job).data,
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# D. Attachments — nested under quote-requests and sent-quotes
# ---------------------------------------------------------------------------


class QuoteRequestAttachmentViewSet(viewsets.ModelViewSet):
    """
    GET/POST /quote-requests/{id}/attachments/
    DELETE /quote-requests/{id}/attachments/{pk}/

    Customer: add/delete when draft; list always.
    Shop: list only (via quote_request id from incoming-requests).
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete"]

    def get_quote_request(self):
        return get_object_or_404(QuoteRequest, pk=self.kwargs["quote_request_pk"])

    def _check_access(self):
        qr = self.get_quote_request()
        user = self.request.user
        is_buyer = qr.created_by_id == user.id
        is_seller = qr.shop_id and qr.shop.owner_id == user.id
        if user.is_staff:
            return qr, True, True  # qr, can_write, can_read
        if is_buyer:
            return qr, qr.status == QuoteStatus.DRAFT, True
        if is_seller:
            return qr, False, True
        return qr, False, False

    def get_queryset(self):
        qr, _, can_read = self._check_access()
        if not can_read:
            return QuoteRequestAttachment.objects.none()
        return QuoteRequestAttachment.objects.filter(quote_request=qr)

    def get_serializer_class(self):
        if self.action == "create":
            return QuoteRequestAttachmentUploadSerializer
        return QuoteRequestAttachmentSerializer

    def list(self, request, *args, **kwargs):
        _, _, can_read = self._check_access()
        if not can_read:
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        qr, can_write, _ = self._check_access()
        if not can_write:
            return Response(
                {"detail": "Only the customer can add attachments, and only when the request is in draft."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        att = serializer.save(quote_request=qr)
        return Response(
            QuoteRequestAttachmentSerializer(att).data,
            status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        qr, can_write, _ = self._check_access()
        if not can_write:
            return Response(
                {"detail": "Only the customer can remove attachments, and only when the request is in draft."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().destroy(request, *args, **kwargs)


class ShopQuoteAttachmentViewSet(viewsets.ModelViewSet):
    """
    GET/POST /sent-quotes/{id}/attachments/
    DELETE /sent-quotes/{id}/attachments/{pk}/

    Shop owner only. Ownership enforced via get_queryset and perform_create.
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete"]

    def get_queryset(self):
        shop_quote_pk = self.kwargs["shop_quote_pk"]
        user = self.request.user
        qs = ShopQuoteAttachment.objects.filter(shop_quote_id=shop_quote_pk)
        if not user.is_staff:
            qs = qs.filter(shop_quote__shop__owner=user)
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return ShopQuoteAttachmentUploadSerializer
        return ShopQuoteAttachmentSerializer

    def perform_create(self, serializer):
        shop_quote = get_object_or_404(
            ShopQuote.objects.filter(shop__owner=self.request.user)
            if not self.request.user.is_staff
            else ShopQuote.objects.all(),
            pk=self.kwargs["shop_quote_pk"],
        )
        serializer.save(shop_quote=shop_quote)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            ShopQuoteAttachmentSerializer(serializer.instance).data,
            status=status.HTTP_201_CREATED,
        )


class ClientMessageInboxViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def _base_queryset(self, request):
        return QuoteRequestMessage.objects.filter(
            quote_request__created_by=request.user,
            recipient_role=QuoteRequestMessage.RecipientRole.CLIENT,
        ).select_related("quote_request", "shop", "shop_quote")

    def list(self, request):
        queryset = self._base_queryset(request).filter(
            direction=QuoteRequestMessage.Direction.INBOUND,
        ).order_by("-sent_at", "-created_at", "-id")
        return Response(QuoteInboxMessageSerializer(queryset, many=True, context={"request": request}).data)

    @action(detail=False, methods=["get"], url_path="outbox")
    def outbox(self, request):
        queryset = self._base_queryset(request).filter(
            direction=QuoteRequestMessage.Direction.OUTBOUND,
        ).order_by("-sent_at", "-created_at", "-id")
        return Response(QuoteInboxMessageSerializer(queryset, many=True, context={"request": request}).data)

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        count = self._base_queryset(request).filter(
            direction=QuoteRequestMessage.Direction.INBOUND,
            read_at__isnull=True,
        ).count()
        return Response({"unread_count": count})

    @action(detail=True, methods=["post"], url_path="read")
    def read(self, request, pk=None):
        message = get_object_or_404(self._base_queryset(request), pk=pk)
        mark_message_read(message)
        return Response(QuoteInboxMessageSerializer(message, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        updated = mark_messages_read(
            self._base_queryset(request).filter(direction=QuoteRequestMessage.Direction.INBOUND)
        )
        return Response({"marked_read": updated})


class ShopMessageInboxViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def _accessible_shop_ids(self, request):
        return list(
            Shop.objects.filter(
                Q(owner=request.user) | Q(memberships__user=request.user, memberships__is_active=True)
            ).values_list("id", flat=True).distinct()
        )

    def _base_queryset(self, request):
        return QuoteRequestMessage.objects.filter(
            shop_id__in=self._accessible_shop_ids(request),
            recipient_role=QuoteRequestMessage.RecipientRole.SHOP_OWNER,
        ).select_related("quote_request", "shop", "shop_quote")

    def list(self, request):
        queryset = self._base_queryset(request).filter(
            direction=QuoteRequestMessage.Direction.INBOUND,
        ).order_by("-sent_at", "-created_at", "-id")
        return Response(QuoteInboxMessageSerializer(queryset, many=True, context={"request": request}).data)

    @action(detail=False, methods=["get"], url_path="outbox")
    def outbox(self, request):
        queryset = self._base_queryset(request).filter(
            direction=QuoteRequestMessage.Direction.OUTBOUND,
        ).order_by("-sent_at", "-created_at", "-id")
        return Response(QuoteInboxMessageSerializer(queryset, many=True, context={"request": request}).data)

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        count = self._base_queryset(request).filter(
            direction=QuoteRequestMessage.Direction.INBOUND,
            read_at__isnull=True,
        ).count()
        return Response({"unread_count": count})

    @action(detail=True, methods=["post"], url_path="read")
    def read(self, request, pk=None):
        message = get_object_or_404(self._base_queryset(request), pk=pk)
        mark_message_read(message)
        return Response(QuoteInboxMessageSerializer(message, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        updated = mark_messages_read(
            self._base_queryset(request).filter(direction=QuoteRequestMessage.Direction.INBOUND)
        )
        return Response({"marked_read": updated})
