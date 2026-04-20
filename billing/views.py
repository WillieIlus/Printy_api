"""Billing API views."""
from __future__ import annotations

import logging

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.models import PaymentTransaction, ShopSubscription
from billing.selectors import (
    get_active_subscription_for_owner,
    get_owner_transactions,
    get_public_plans,
    get_subscription_detail,
)
from billing.serializers import (
    DowngradeSubscriptionSerializer,
    InitiateRenewalSerializer,
    MpesaSandboxTestSerializer,
    PaymentTransactionSerializer,
    PublicPlanSerializer,
    ShopSubscriptionSerializer,
    StartSubscriptionSerializer,
    SubscriptionDetailSerializer,
    UpgradeSubscriptionSerializer,
    UsageSerializer,
)
from billing.services.callbacks import handle_mpesa_callback
from billing.services.entitlements import get_current_usage, get_plan_limits
from billing.services.payments import initiate_test_stk_push, reconcile_transaction
from billing.services.subscriptions import (
    cancel_at_period_end,
    get_or_create_free_subscription,
    immediate_cancel,
    request_downgrade,
    request_upgrade,
    renew_subscription,
    subscribe_to_plan,
)

logger = logging.getLogger("payments")


class PlanListView(generics.ListAPIView):
    serializer_class = PublicPlanSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        return get_public_plans()


class SubscriptionView(APIView):
    def get(self, request):
        sub = get_subscription_detail(request.user)
        if sub is None:
            sub = get_or_create_free_subscription(request.user)
        return Response(SubscriptionDetailSerializer(sub).data)


class UsageView(APIView):
    def get(self, request):
        sub = get_active_subscription_for_owner(request.user)
        usage = get_current_usage(request.user)
        # get_active_subscription_for_owner always returns a subscription (creates Free if needed),
        # but we provide explicit None defaults so the serializer never receives missing keys.
        null_limits = {
            "shops_limit": None,
            "machines_limit": None,
            "products_limit": None,
            "quotes_per_month_limit": None,
            "users_limit": None,
        }
        limits = get_plan_limits(sub) if sub else null_limits
        return Response(UsageSerializer({**null_limits, **usage, **limits}).data)


class SubscribeView(APIView):
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


class UpgradeView(APIView):
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


class DowngradeView(APIView):
    def post(self, request):
        serializer = DowngradeSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            sub = request_downgrade(owner=request.user, target_plan_code=data["target_plan_code"])
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "message": f"Downgrade scheduled at end of current period ({sub.ends_at}).",
                "subscription": ShopSubscriptionSerializer(sub).data,
            }
        )


class CancelView(APIView):
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


class ReactivateView(APIView):
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


class InitiateRenewalView(APIView):
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

        txn = renew_subscription(sub)
        if txn is None:
            return Response({"detail": "Renewal could not be initiated."}, status=400)

        return Response(
            {
                "message": "Renewal STK push sent.",
                "transaction": PaymentTransactionSerializer(txn).data,
            }
        )


class MpesaSandboxTestStkView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = MpesaSandboxTestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            txn = initiate_test_stk_push(
                owner=request.user,
                phone_number=serializer.validated_data["phone_number"],
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Failed to initiate sandbox test STK push for user=%s: %s", request.user.id, exc)
            return Response(
                {"detail": "Could not initiate sandbox STK push at this time."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        response_status = status.HTTP_201_CREATED
        if txn.status == PaymentTransaction.STATUS_FAILED:
            response_status = status.HTTP_502_BAD_GATEWAY

        return Response(
            {
                "message": "Sandbox STK push initiated. Amount is fixed at KES 1. Approve on your phone.",
                "transaction": PaymentTransactionSerializer(txn).data,
            },
            status=response_status,
        )


class MpesaCallbackView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        stk = payload.get("Body", {}).get("stkCallback", {}) if isinstance(payload, dict) else {}
        logger.info(
            "Received billing M-Pesa callback checkout_request_id=%s merchant_request_id=%s",
            stk.get("CheckoutRequestID"),
            stk.get("MerchantRequestID"),
        )
        result = handle_mpesa_callback(payload)
        return Response(result, status=status.HTTP_200_OK)


class PaymentListView(generics.ListAPIView):
    serializer_class = PaymentTransactionSerializer

    def get_queryset(self):
        return get_owner_transactions(self.request.user)


class PaymentDetailView(generics.RetrieveAPIView):
    serializer_class = PaymentTransactionSerializer

    def get_queryset(self):
        return PaymentTransaction.objects.filter(owner=self.request.user)


class PaymentReconcileView(APIView):
    def post(self, request, pk: int):
        try:
            txn = PaymentTransaction.objects.get(pk=pk, owner=request.user)
        except PaymentTransaction.DoesNotExist:
            return Response({"detail": "Payment not found."}, status=status.HTTP_404_NOT_FOUND)

        if txn.status == PaymentTransaction.STATUS_SUCCESS:
            return Response(
                {
                    "message": "Payment already marked successful.",
                    "transaction": PaymentTransactionSerializer(txn).data,
                }
            )

        try:
            query_response = reconcile_transaction(txn)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Failed to reconcile payment %s: %s", txn.id, exc)
            return Response(
                {"detail": "Could not query Daraja at this time."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        txn.refresh_from_db()
        return Response(
            {
                "message": "Payment reconciliation completed.",
                "transaction": PaymentTransactionSerializer(txn).data,
                "daraja_response": query_response,
            }
        )


class AdminManualActivateView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def post(self, request):
        sub_id = request.data.get("subscription_id")
        try:
            sub = ShopSubscription.objects.get(id=sub_id)
        except ShopSubscription.DoesNotExist:
            return Response({"detail": "Subscription not found."}, status=404)

        import uuid
        from billing.services.subscriptions import activate_subscription_from_successful_payment

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
        activate_subscription_from_successful_payment(txn)
        return Response({"detail": "Subscription activated.", "subscription_id": sub.id})


class AdminManualSuspendView(APIView):
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
