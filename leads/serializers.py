from rest_framework import serializers
from .models import ShopLead


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
