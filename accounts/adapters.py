from django.conf import settings
from allauth.account.adapter import DefaultAccountAdapter


class AccountAdapter(DefaultAccountAdapter):
    """
    Redirect allauth email links to the frontend SPA instead of the Django
    form-based views.  This prevents confirmation/reset URLs from pointing at
    the API host or at localhost in production.
    """

    def get_email_confirmation_url(self, request, emailconfirmation):
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
        return f"{frontend_url}/auth/confirm-email?key={emailconfirmation.key}"

    def get_reset_password_url(self, request):
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
        return f"{frontend_url}/auth/reset-password"

    def get_reset_password_from_key_url(self, key):
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
        return f"{frontend_url}/auth/reset-password?key={key}"
