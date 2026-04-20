from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    ConfirmEmailView,
    CustomTokenObtainPairView,
    RegisterView,
    ResendEmailConfirmationView,
    UserDetailView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("token/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("me/", UserDetailView.as_view(), name="user_detail"),
    path("confirm-email/", ConfirmEmailView.as_view(), name="confirm_email"),
    path("email/verify/", ConfirmEmailView.as_view(), name="email_verify"),
    path("resend-confirmation/", ResendEmailConfirmationView.as_view(), name="resend_confirmation"),
    path("email/resend/", ResendEmailConfirmationView.as_view(), name="email_resend"),
]
