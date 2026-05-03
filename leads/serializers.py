from rest_framework import serializers
from .models import DemoAction, ShopLead


class ShopLeadSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShopLead
        fields = ["shop_name", "phone", "area"]

    def validate_shop_name(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Shop name is required.")
        return cleaned

    def validate_phone(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Phone number is required.")
        return cleaned

    def validate_area(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Area is required.")
        return cleaned


class DemoActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DemoAction
        fields = [
            "selected_product", "quantity", "suggested_min", "suggested_max",
            "action", "modified_min", "modified_max", "reject_reason",
        ]

    def validate_action(self, value):
        allowed = [c[0] for c in DemoAction.Action.choices]
        if value not in allowed:
            raise serializers.ValidationError(f"Action must be one of: {', '.join(allowed)}.")
        return value

    def validate(self, attrs):
        suggested_min = attrs.get("suggested_min")
        suggested_max = attrs.get("suggested_max")
        action = attrs.get("action")
        modified_min = attrs.get("modified_min")
        modified_max = attrs.get("modified_max")
        reject_reason = (attrs.get("reject_reason") or "").strip()

        if suggested_min is not None and suggested_max is not None and suggested_min > suggested_max:
            raise serializers.ValidationError({"suggested_max": "Suggested max must be greater than or equal to suggested min."})

        if action == DemoAction.Action.MODIFIED:
            if modified_min is None or modified_max is None:
                raise serializers.ValidationError({"modified_min": "Modified range is required when action is modified."})
            if modified_min > modified_max:
                raise serializers.ValidationError({"modified_max": "Modified max must be greater than or equal to modified min."})

        if action == DemoAction.Action.REJECTED:
            attrs["reject_reason"] = reject_reason or "No reason given"
        else:
            attrs["reject_reason"] = reject_reason

        return attrs
