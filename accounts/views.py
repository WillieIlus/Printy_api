import logging
import os

import requests as http_requests
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import User
from .serializers import CustomTokenObtainPairSerializer, UserCreateSerializer, UserSerializer


logger = logging.getLogger("api.auth")


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not local or not domain:
        return "***"
    if len(local) == 1:
        masked_local = "*"
    elif len(local) == 2:
        masked_local = f"{local[0]}*"
    else:
        masked_local = f"{local[0]}***{local[-1]}"
    return f"{masked_local}@{domain}"


class RegisterView(generics.CreateAPIView):
    """Register a new user (buyer or seller)."""

    permission_classes = [AllowAny]
    serializer_class = UserCreateSerializer


class CustomTokenObtainPairView(TokenObtainPairView):
    """JWT token obtain view that accepts username or email."""

    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]


class UserDetailView(generics.RetrieveUpdateAPIView):
    """Current user profile."""

    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class ConfirmEmailView(APIView):
    """Accept the key from the confirmation email and mark the address verified."""

    permission_classes = [AllowAny]

    def post(self, request):
        key = request.data.get("key", "").strip()
        if not key:
            return Response(
                {"detail": "key is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        from allauth.account.models import EmailConfirmationHMAC, EmailConfirmation

        confirmation = EmailConfirmationHMAC.from_key(key)
        if confirmation is None:
            confirmation = EmailConfirmation.from_key(key)

        if confirmation is None:
            logger.info("email_verification_confirm_invalid_key")
            return Response(
                {"detail": "Invalid or expired confirmation key."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email_address = confirmation.confirm(request)
        if not email_address:
            logger.info("email_verification_confirm_failed")
            return Response(
                {"detail": "Invalid or expired confirmation key."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.info(
            "email_verification_confirmed email=%s user_id=%s",
            _mask_email(email_address.email),
            email_address.user_id,
        )
        return Response(
            {
                "detail": "Email confirmed successfully.",
                "email": email_address.email,
                "verified": True,
            }
        )


class ResendEmailConfirmationView(APIView):
    """Re-send the confirmation email for an unverified address."""

    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email", "").strip().lower()
        if not email:
            return Response(
                {"detail": "email is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        from allauth.account.models import EmailAddress

        sent = False
        try:
            email_address = EmailAddress.objects.get(email__iexact=email, verified=False)
            email_address.send_confirmation(request)
            sent = True
        except EmailAddress.DoesNotExist:
            pass

        logger.info(
            "email_verification_resend_requested email=%s outcome=%s",
            _mask_email(email),
            "sent" if sent else "noop",
        )
        return Response(
            {
                "detail": "If that address exists and is unverified, a new confirmation email has been sent.",
                "sent": sent,
            }
        )


class GoogleSocialLoginView(APIView):
    """
    Verify a Google ID token and return JWT tokens for the user.
    Creates the user account on first login.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        id_token = (request.data.get("id_token") or "").strip()
        if not id_token:
            return Response({"detail": "id_token is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            resp = http_requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": id_token},
                timeout=10,
            )
        except http_requests.RequestException:
            return Response({"detail": "Could not verify Google token."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if resp.status_code != 200:
            return Response({"detail": "Invalid Google token."}, status=status.HTTP_400_BAD_REQUEST)

        google_data = resp.json()

        client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        if client_id and google_data.get("aud") != client_id:
            return Response({"detail": "Token audience mismatch."}, status=status.HTTP_400_BAD_REQUEST)

        if str(google_data.get("email_verified", "")).lower() not in ("true", "1"):
            return Response({"detail": "Google account email is not verified."}, status=status.HTTP_400_BAD_REQUEST)

        email = (google_data.get("email") or "").strip().lower()
        if not email:
            return Response({"detail": "Google account has no email address."}, status=status.HTTP_400_BAD_REQUEST)

        name = google_data.get("name", "")
        given_name = google_data.get("given_name", "")
        family_name = google_data.get("family_name", "")
        google_sub = google_data.get("sub", "")
        role = (request.data.get("role") or "client").strip()
        if role not in ("client", "shop_owner"):
            role = "client"

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "name": name,
                "first_name": given_name,
                "last_name": family_name,
                "role": role,
                "is_active": True,
            },
        )

        if not created and not user.name and name:
            user.name = name
            user.save(update_fields=["name", "updated_at"] if hasattr(user, "updated_at") else ["name"])

        from allauth.account.models import EmailAddress
        EmailAddress.objects.get_or_create(
            user=user,
            email=email,
            defaults={"primary": True, "verified": True},
        )

        if google_sub:
            from allauth.socialaccount.models import SocialAccount
            SocialAccount.objects.get_or_create(
                user=user,
                provider="google",
                uid=google_sub,
                defaults={"extra_data": google_data},
            )

        refresh = RefreshToken.for_user(user)
        logger.info("google_social_login email=%s created=%s", _mask_email(email), created)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "name": user.name or name,
                    "role": user.role,
                    "is_email_verified": True,
                },
            }
        )
