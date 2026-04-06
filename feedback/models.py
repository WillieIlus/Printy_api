from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class BetaFeedback(models.Model):
    """Structured feedback from beta testers."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="beta_feedback",
        verbose_name=_("user"),
    )
    message = models.TextField(
        verbose_name=_("message"),
        help_text=_("Required feedback text."),
    )
    rating = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_("rating"),
        help_text=_("Optional 1–5 rating."),
    )
    page = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name=_("page"),
        help_text=_("Route or page context."),
    )
    user_agent = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name=_("user agent"),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("created at"),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("beta feedback")
        verbose_name_plural = _("beta feedback")

    def __str__(self):
        return f"Feedback from {self.user.email} ({self.created_at.date()})"

    def clean(self):
        super().clean()
        if self.rating is not None and (self.rating < 1 or self.rating > 5):
            raise ValidationError({"rating": _("Rating must be between 1 and 5.")})
