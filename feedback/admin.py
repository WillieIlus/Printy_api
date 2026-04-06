from django.contrib import admin
from django.utils.html import format_html

from .models import BetaFeedback


@admin.register(BetaFeedback)
class BetaFeedbackAdmin(admin.ModelAdmin):
    list_display = ["user", "rating", "page", "created_at", "short_message"]
    list_filter = ["rating", "created_at"]
    search_fields = ["user__email", "message", "page"]
    readonly_fields = ["user", "message", "rating", "page", "user_agent", "created_at"]
    date_hierarchy = "created_at"

    def short_message(self, obj):
        if not obj.message:
            return "—"
        text = obj.message[:60] + "…" if len(obj.message) > 60 else obj.message
        return format_html('<span title="{}">{}</span>', obj.message, text)

    short_message.short_description = "Message"
