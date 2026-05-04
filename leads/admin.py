from django.contrib import admin
from .models import EarlyAccessCampaign, ShopLead


@admin.register(ShopLead)
class ShopLeadAdmin(admin.ModelAdmin):
    list_display = ["shop_name", "phone", "area", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["shop_name", "phone", "area"]
    readonly_fields = ["created_at", "updated_at"]
    list_per_page = 50


@admin.register(EarlyAccessCampaign)
class EarlyAccessCampaignAdmin(admin.ModelAdmin):
    list_display = ["city", "total_spots", "manual_reserved_spots", "active", "created_at"]
    list_filter = ["active"]
    readonly_fields = ["created_at", "updated_at"]
