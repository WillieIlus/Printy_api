from django.conf import settings
from django.db import models
from common.models import TimeStampedModel


class EarlyAccessCampaign(TimeStampedModel):
    city = models.CharField(max_length=100, unique=True)
    total_spots = models.PositiveIntegerField(default=20)
    manual_reserved_spots = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Early Access Campaign"
        verbose_name_plural = "Early Access Campaigns"

    def __str__(self):
        return f"{self.city} ({self.total_spots} spots)"


class ShopLead(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONTACTED = "CONTACTED", "Contacted"
        ONBOARDED = "ONBOARDED", "Onboarded"
        REJECTED = "REJECTED", "Rejected"

    shop_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=50)
    area = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Shop Lead"
        verbose_name_plural = "Shop Leads"

    def __str__(self):
        return f"{self.shop_name} ({self.phone})"


class DemoAction(TimeStampedModel):
    class Action(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        MODIFIED = "modified", "Modified"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="demo_actions",
    )
    selected_product = models.CharField(max_length=50)
    quantity = models.PositiveIntegerField()
    suggested_min = models.PositiveIntegerField()
    suggested_max = models.PositiveIntegerField()
    action = models.CharField(max_length=20, choices=Action.choices)
    modified_min = models.PositiveIntegerField(null=True, blank=True)
    modified_max = models.PositiveIntegerField(null=True, blank=True)
    reject_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Demo Action"
        verbose_name_plural = "Demo Actions"

    def __str__(self):
        return f"{self.selected_product} – {self.action} ({self.created_at:%Y-%m-%d})"
