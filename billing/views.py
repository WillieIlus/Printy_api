"""Billing API views."""
from __future__ import annotations

import logging

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.models import PaymentTransaction, Plan, ShopSubscription
from billing.selectors import (
    get_active_subscription_for_owner,
    get_owner_transactions,
    get_public_plans,
    get_subscription_detail,
)
from billing.serializers import (
    DowngradeSubscriptionSerializer,
    InitiateRenewalSerializer,
    PaymentTransactionSerializer,
    PlanSerializer,
    PublicPlanSerializer,
    ShopSubscriptionSerializer,
    StartSubscriptionSerializer,
    SubscriptionDetailSerializer,
    UpgradeSubscriptionSerializer,
    UsageSerializer,
)
from billing.services.callbacks import handle_mpesa_callback
from billing.services.entitlements import get_current_usage, get_plan_limits
from billing.services.subscriptions import (
    cancel_at_period_end,
    get_or_create_free_subscription,
    immediate_cancel,
    request_downgrade,
    request_upgrade,
    subscribe_to_plan,
)

logger = logging.getLogger("payments")


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

class PlanListView(generics.ListAPIView):
    """GET /api/billing/plans/ — public, no auth required."""
    serializer_class = PublicPlanSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        return get_public_plans()


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

class SubscriptionView(APIView):
    """GET /api/billing/subscription/ — return caller's active subscription."""

    def get(self, request):
        sub = get_subscription_detail(request.user)
        if sub is None:
            sub = get_or_create_free_subscription(request.user)
        serializer = SubscriptionDetailSerializer(sub)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

class UsageView(APIView):
    """GET /api/billing/usage/"""

    def get(self, request):
        sub = get_active_subscription_for_owner(request.user)
        usage = get_current_usage(request.user)
        limits = get_plan_limits(sub) if sub else {}
        data = {**usage, **limits}
        serializer = UsageSerializer(data)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Subscribe (new subscription / re-subscribe)
# ---------------------------------------------------------------------------

class SubscribeView(APIView):
    """POST /api/billing/subscribe/"""

    def post(self, request):
        serializer = StartSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            txn = subscribe_to_plan(
                owner=request.user,
                plan_code=data["plan_code"],
                billing_interval=data["billing_interval"],
                phone_number=data["phone_number"],
                selected_shop_ids=data["selected_shop_ids"],
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "message": "STK push initiated. Approve on your phone.",
                "transaction": PaymentTransactionSerializer(txn).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

class UpgradeView(APIView):
    """POST /api/billing/upgrade/"""

    def post(self, request):
        serializer = UpgradeSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            txn = request_upgrade(
                owner=request.user,
                target_plan_code=data["target_plan_code"],
                billing_interval=data["billing_interval"],
                phone_number=data.get("phone_number") or None,
                selected_shop_ids=data.get("selected_shop_ids") or None,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "message": "Upgrade STK push initiated. Approve on your phone.",
                "transaction": PaymentTransactionSerializer(txn).data,
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------

class DowngradeView(APIView):
    """POST /api/billing/downgrade/"""

    def post(self, request):
        serializer = DowngradeSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            sub = request_downgrade(
                owner=request.user,
                target_plan_code=data["target_plan_code"],
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "message": f"Downgrade scheduled at end of current period ({sub.ends_at}).",
                "subscription": ShopSubscriptionSerializer(sub).data,
            }
        )


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

class CancelView(APIView):
    """POST /api/billing/cancel/"""

    def post(self, request):
        immediate = request.data.get("immediate", False)
        try:
            if immediate:
                sub = immediate_cancel(request.user)
                msg = "Subscription cancelled immediately."
            else:
                sub = cancel_at_period_end(request.user)
                msg = "Subscription will cancel at end of current period."
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": msg, "subscription": ShopSubscriptionSerializer(sub).data})


# ---------------------------------------------------------------------------
# Reactivate (manual renewal trigger)
# ---------------------------------------------------------------------------

class ReactivateView(APIView):
    """POST /api/billing/reactivate/ — re-enable auto renew or fire a manual STK push."""

    def post(self, request):
        serializer = InitiateRenewalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        sub = (
            ShopSubscription.objects.filter(
                owner=request.user,
                status__in=[
                    ShopSubscription.STATUS_SUSPENDED,
                    ShopSubscription.STATUS_PAST_DUE,
                    ShopSubscription.STATUS_GRACE,
                    ShopSubscription.STATUS_CANCELLED,
                ],
            )
            .select_related("plan")
            .first()
        )
        if sub is None:
            return Response({"detail": "No suspended/cancelled subscription to reactivate."}, status=400)

        from billing.services.subscriptions import renew_subscription
        if data.get("phone_number"):
            sub.payment_phone_e164 = data["phone_number"]
            sub.save(update_fields=["payment_phone_e164", "updated_at"])

        sub.auto_renew_enabled = True
        sub.save(update_fields=["auto_renew_enabled", "updated_at"])

        txn = renew_subscription(sub)
        if txn is None:
            return Response({"detail": "Free plan does not require payment."})

        return Response(
            {
                "message": "Reactivation STK push initiated.",
                "transaction": PaymentTransactionSerializer(txn).data,
            }
        )


# ---------------------------------------------------------------------------
# Manual renewal
# ---------------------------------------------------------------------------

class InitiateRenewalView(APIView):
    """POST /api/billing/initiate-renewal/"""

    def post(self, request):
        serializer = InitiateRenewalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        sub = get_active_subscription_for_owner(request.user)
        if sub is None or sub.plan.is_free:
            return Response({"detail": "No paid subscription to renew."}, status=400)

        if data.get("phone_number"):
            sub.payment_phone_e164 = data["phone_number"]
            sub.save(update_fields=["payment_phone_e164", "updated_at"])

        from billing.services.subscriptions import renew_subscription
        txn = renew_subscription(sub)
        if txn is None:
            return Response({"detail": "Renewal could not be initiated."}, status=400)

        return Response(
            {
                "message": "Renewal STK push sent.",
                "transaction": PaymentTransactionSerializer(txn).data,
            }
        )


# ---------------------------------------------------------------------------
# M-Pesa callback (public endpoint — Safaricom calls this)
# ---------------------------------------------------------------------------

class MpesaCallbackView(APIView):
    """POST /api/billing/mpesa/callback/ — Daraja STK push result."""
    permission_classes = [permissions.AllowAny]
    authentication_classes = []  # Daraja cannot send a Bearer token

    def post(self, request):
        payload = request.data
        logger.info("Received M-Pesa callback: %s", str(payload)[:300])
        result = handle_mpesa_callback(payload)
        # Always return 200 to prevent Daraja retry storms
        return Response(result, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Payment history
# ---------------------------------------------------------------------------

class PaymentListView(generics.ListAPIView):
    """GET /api/billing/payments/"""
    serializer_class = PaymentTransactionSerializer

    def get_queryset(self):
        return get_owner_transactions(self.request.user)


class PaymentDetailView(generics.RetrieveAPIView):
    """GET /api/billing/payments/{id}/"""
    serializer_class = PaymentTransactionSerializer

    def get_queryset(self):
        return PaymentTransaction.objects.filter(owner=self.request.user)


# ---------------------------------------------------------------------------
# Admin / support actions
# ---------------------------------------------------------------------------

class AdminManualActivateView(APIView):
    """POST /api/billing/admin/manual-activate/ — staff only."""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request):
        sub_id = request.data.get("subscription_id")
        try:
            sub = ShopSubscription.objects.get(id=sub_id)
        except ShopSubscription.DoesNotExist:
            return Response({"detail": "Subscription not found."}, status=404)

        from billing.services.subscriptions import activate_subscription_from_successful_payment
        from billing.services.payments import build_idempotency_key
        import uuid

        # Create a synthetic manual transaction
        txn = PaymentTransaction.objects.create(
            subscription=sub,
            owner=sub.owner,
            plan=sub.plan,
            transaction_type=PaymentTransaction.TYPE_MANUAL_RETRY,
            provider=PaymentTransaction.PROVIDER_MPESA,
            provider_method=PaymentTransaction.METHOD_MANUAL,
            amount=sub.plan.get_price(sub.billing_interval),
            currency="KES",
            status=PaymentTransaction.STATUS_SUCCESS,
            idempotency_key=f"manual-{sub.id}-{uuid.uuid4().hex[:16]}",
            result_desc="Manual activation by admin",
        )
        from billing.services.subscriptions import activate_subscription_from_successful_payment
        activate_subscription_from_successful_payment(txn)
        return Response({"detail": "Subscription activated.", "subscription_id": sub.id})


class AdminManualSuspendView(APIView):
    """POST /api/billing/admin/manual-suspend/ — staff only."""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request):
        sub_id = request.data.get("subscription_id")
        reason = request.data.get("reason", "Admin manual suspension")
        try:
            sub = ShopSubscription.objects.get(id=sub_id)
        except ShopSubscription.DoesNotExist:
            return Response({"detail": "Subscription not found."}, status=404)

        sub.mark_suspended()
        sub.notes = (sub.notes + f"\n{reason}").strip()
        sub.save(update_fields=["notes", "updated_at"])
        return Response({"detail": "Subscription suspended.", "subscription_id": sub.id})
