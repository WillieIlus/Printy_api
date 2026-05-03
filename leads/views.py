from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import ShopLead
from .serializers import ShopLeadSerializer

_ACTIVE = [ShopLead.Status.PENDING, ShopLead.Status.CONTACTED, ShopLead.Status.ONBOARDED]


class LeadSubmitThrottle(AnonRateThrottle):
    scope = "lead_submit"
    rate = "10/hour"


class SpotsView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        total = getattr(settings, "EARLY_ACCESS_TOTAL_SPOTS", 20)
        used = min(ShopLead.objects.filter(status__in=_ACTIVE).count(), total)
        return Response({"total": total, "used": used, "remaining": max(total - used, 0)})


class ApplyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LeadSubmitThrottle]

    def post(self, request):
        serializer = ShopLeadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        total = getattr(settings, "EARLY_ACCESS_TOTAL_SPOTS", 20)
        if ShopLead.objects.filter(status__in=_ACTIVE).count() >= total:
            return Response(
                {"detail": "No spots remaining. Please check back later."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer.save()
        return Response({"detail": "Application received."}, status=status.HTTP_201_CREATED)
