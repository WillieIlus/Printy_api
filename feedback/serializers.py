from rest_framework import serializers

from .models import BetaFeedback


class BetaFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = BetaFeedback
        fields = ["message", "rating", "page", "user_agent"]
        extra_kwargs = {
            "message": {"required": True},
            "rating": {"required": False},
            "page": {"required": False},
            "user_agent": {"required": False},
        }

    def validate_message(self, value):
        if not value or len(value.strip()) < 10:
            raise serializers.ValidationError("Message must be at least 10 characters.")
        return value.strip()

    def validate_rating(self, value):
        if value is not None and (value < 1 or value > 5):
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value
