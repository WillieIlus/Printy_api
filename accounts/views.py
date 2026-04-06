from rest_framework import generics
from rest_framework.permissions import AllowAny
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
