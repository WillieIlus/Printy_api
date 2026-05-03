from django.db import models
from common.models import TimeStampedModel


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
