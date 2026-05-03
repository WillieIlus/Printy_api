from django.contrib import admin
from .models import ShopLead


@admin.register(ShopLead)
class ShopLeadAdmin(admin.ModelAdmin):
    list_display = ["shop_name", "phone", "area", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["shop_name", "phone", "area"]
    readonly_fields = ["created_at", "updated_at"]
    list_per_page = 50
