import logging

from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
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
