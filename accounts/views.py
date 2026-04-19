from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import User
from .serializers import CustomTokenObtainPairSerializer, UserCreateSerializer, UserSerializer


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
            return Response(
                {"detail": "Invalid or expired confirmation key."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email_address = confirmation.confirm(request)
        if not email_address:
            return Response(
                {"detail": "Invalid or expired confirmation key."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"detail": "Email confirmed successfully."})


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

        try:
            email_address = EmailAddress.objects.get(email__iexact=email, verified=False)
            email_address.send_confirmation(request)
        except EmailAddress.DoesNotExist:
            pass

        return Response(
            {"detail": "If that address exists and is unverified, a new confirmation email has been sent."}
        )
