from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import BetaFeedbackViewSet

router = DefaultRouter()
router.register(r"beta-feedback", BetaFeedbackViewSet, basename="beta-feedback")

urlpatterns = [
    path("", include(router.urls)),
]
