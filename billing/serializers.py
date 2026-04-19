"""Billing serializers."""
from __future__ import annotations

from rest_framework import serializers

from billing.models import (
    Plan,
    PaymentTransaction,
    ShopSubscription,
    SubscriptionShop,
)
from billing.services.payments import normalize_phone_number


class PublicPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            "id",
            "code",
            "name",
            "price_monthly",
            "price_annual",
            "currency",
            "shops_limit",
            "machines_limit",
            "products_limit",
            "quotes_per_month_limit",
            "users_limit",
            "all_papers_enabled",
            "branded_quotes_enabled",
            "customer_history_enabled",
            "analytics_level",
            "priority_support",
            "sort_order",
            "public_tagline",
            "best_for",
            "benefits",
        ]


class PlanSerializer(PublicPlanSerializer):
    class Meta(PublicPlanSerializer.Meta):
        fields = PublicPlanSerializer.Meta.fields + ["is_active", "metadata", "created_at", "updated_at"]


class SubscriptionShopSerializer(serializers.ModelSerializer):
    shop_name = serializers.CharField(source="shop.name", read_only=True)
    shop_slug = serializers.CharField(source="shop.slug", read_only=True)

    class Meta:
        model = SubscriptionShop
        fields = ["id", "shop", "shop_name", "shop_slug", "is_primary", "added_at"]


class ShopSubscriptionSerializer(serializers.ModelSerializer):
    plan = PublicPlanSerializer(read_only=True)
    shops = SubscriptionShopSerializer(source="subscription_shops", many=True, read_only=True)
    is_active_now = serializers.SerializerMethodField()
    remaining_days = serializers.SerializerMethodField()

    class Meta:
        model = ShopSubscription
        fields = [
            "id",
            "plan",
            "billing_interval",
            "status",
            "starts_at",
            "ends_at",
            "renews_at",
            "auto_renew_enabled",
            "grace_period_ends_at",
            "over_limit",
            "payment_phone_e164",
            "is_active_now",
            "remaining_days",
            "shops",
            "created_at",
            "updated_at",
        ]

    def get_is_active_now(self, obj: ShopSubscription) -> bool:
        return obj.is_active_now()

    def get_remaining_days(self, obj: ShopSubscription) -> int:
        return obj.remaining_days()


class SubscriptionDetailSerializer(ShopSubscriptionSerializer):
    class Meta(ShopSubscriptionSerializer.Meta):
        fields = ShopSubscriptionSerializer.Meta.fields + [
            "cancellation_requested_at",
            "cancelled_at",
            "suspended_at",
            "last_renewal_attempt_at",
            "next_renewal_attempt_at",
            "mpesa_reference_last",
            "notes",
        ]


class PaymentTransactionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source="plan.name", read_only=True, default="")

    class Meta:
        model = PaymentTransaction
        fields = [
            "id",
            "transaction_type",
            "provider",
            "provider_method",
            "plan",
            "plan_name",
            "amount",
            "currency",
            "status",
            "phone_number",
            "account_reference",
            "transaction_desc",
            "merchant_request_id",
            "checkout_request_id",
            "response_code",
            "response_description",
            "customer_message",
            "result_code",
            "result_desc",
            "mpesa_receipt_number",
            "initiated_at",
            "callback_received_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


def _validate_phone(value: str) -> str:
    try:
        return normalize_phone_number(value)
    except ValueError as exc:
        raise serializers.ValidationError(str(exc))


def _validate_plan_code(value: str) -> str:
    valid = [Plan.CODE_FREE, Plan.CODE_BIASHARA, Plan.CODE_BIASHARA_PLUS, Plan.CODE_BIASHARA_MAX]
    if value not in valid:
        raise serializers.ValidationError(f"Invalid plan code. Choose from: {valid}")
    return value


class StartSubscriptionSerializer(serializers.Serializer):
    plan_code = serializers.CharField()
    billing_interval = serializers.ChoiceField(
        choices=[ShopSubscription.INTERVAL_MONTHLY, ShopSubscription.INTERVAL_ANNUAL],
        default=ShopSubscription.INTERVAL_MONTHLY,
    )
    phone_number = serializers.CharField(max_length=20)
    selected_shop_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)

    def validate_plan_code(self, value):
        return _validate_plan_code(value)

    def validate_phone_number(self, value):
        return _validate_phone(value)

    def validate(self, attrs):
        plan_code = attrs["plan_code"]
        selected = attrs["selected_shop_ids"]

        if plan_code == Plan.CODE_FREE:
            raise serializers.ValidationError({"plan_code": "Cannot subscribe to Free via this endpoint."})

        limit_map = {
            Plan.CODE_BIASHARA: 1,
            Plan.CODE_BIASHARA_PLUS: 2,
            Plan.CODE_BIASHARA_MAX: 5,
        }
        limit = limit_map.get(plan_code, 1)
        if len(selected) > limit:
            raise serializers.ValidationError(
                {"selected_shop_ids": f"Plan {plan_code} allows max {limit} shop(s)."}
            )

        try:
            plan = Plan.objects.get(code=plan_code, is_active=True)
        except Plan.DoesNotExist:
            raise serializers.ValidationError({"plan_code": "This plan is not currently available."})
        attrs["plan"] = plan
        return attrs


class UpgradeSubscriptionSerializer(serializers.Serializer):
    target_plan_code = serializers.CharField()
    billing_interval = serializers.ChoiceField(
        choices=[ShopSubscription.INTERVAL_MONTHLY, ShopSubscription.INTERVAL_ANNUAL],
        default=ShopSubscription.INTERVAL_MONTHLY,
    )
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    selected_shop_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)

    def validate_target_plan_code(self, value):
        return _validate_plan_code(value)

    def validate_phone_number(self, value):
        if not value:
            return value
        return _validate_phone(value)

    def validate(self, attrs):
        plan_code = attrs["target_plan_code"]
        if plan_code == Plan.CODE_FREE:
            raise serializers.ValidationError({"target_plan_code": "Use downgrade endpoint to move to Free."})

        selected = attrs.get("selected_shop_ids", [])
        if selected:
            limit_map = {
                Plan.CODE_BIASHARA: 1,
                Plan.CODE_BIASHARA_PLUS: 2,
                Plan.CODE_BIASHARA_MAX: 5,
            }
            limit = limit_map.get(plan_code, 1)
            if len(selected) > limit:
                raise serializers.ValidationError(
                    {"selected_shop_ids": f"Plan {plan_code} allows max {limit} shop(s)."}
                )
        return attrs


class DowngradeSubscriptionSerializer(serializers.Serializer):
    target_plan_code = serializers.CharField()

    def validate_target_plan_code(self, value):
        return _validate_plan_code(value)


class InitiateRenewalSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_phone_number(self, value):
        if not value:
            return value
        return _validate_phone(value)


class UsageSerializer(serializers.Serializer):
    shops = serializers.IntegerField()
    machines = serializers.IntegerField()
    active_products = serializers.IntegerField()
    team_members = serializers.IntegerField()
    quotes_this_month = serializers.IntegerField()
    shops_limit = serializers.IntegerField(allow_null=True)
    machines_limit = serializers.IntegerField(allow_null=True)
    products_limit = serializers.IntegerField(allow_null=True)
    quotes_per_month_limit = serializers.IntegerField(allow_null=True)
    users_limit = serializers.IntegerField(allow_null=True)
