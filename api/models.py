from django.conf import settings
from django.db import models
from django.db.models import Q

from common.models import TimeStampedModel


class PartnerClient(TimeStampedModel):
    partner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="partner_clients",
    )
    client_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_by_partners",
    )
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=50, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    company = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["partner", "client_user"],
                condition=Q(client_user__isnull=False),
                name="api_partnerclient_unique_partner_client_user",
            ),
            models.UniqueConstraint(
                fields=["partner", "phone"],
                condition=~Q(phone=""),
                name="api_partnerclient_unique_partner_phone",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.partner_id}:{self.name}"
