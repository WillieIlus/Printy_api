from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import DemoAction, EarlyAccessCampaign, ShopLead
from .serializers import DemoActionSerializer, ShopLeadSerializer

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


class EarlyAccessView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        city = request.query_params.get("city", "Nairobi")
        campaign = EarlyAccessCampaign.objects.filter(city__iexact=city, active=True).first()

        if campaign:
            total_spots = campaign.total_spots
            manual_reserved = campaign.manual_reserved_spots
        else:
            total_spots = getattr(settings, "EARLY_ACCESS_TOTAL_SPOTS", 20)
            manual_reserved = 0

        onboarded_count = ShopLead.objects.filter(status=ShopLead.Status.ONBOARDED).count()
        active_count = ShopLead.objects.filter(status__in=_ACTIVE).count()
        claimed_spots = min(active_count + manual_reserved, total_spots)
        remaining = max(total_spots - claimed_spots, 0)

        return Response({
            "city": city,
            "total_spots": total_spots,
            "claimed_spots": claimed_spots,
            "remaining_spots": remaining,
            "manual_reserved_spots": manual_reserved,
            "onboarded_shop_count": onboarded_count,
        })


class DemoActionView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = DemoActionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        user = request.user if request.user.is_authenticated else None
        serializer.save(user=user)
        return Response({"detail": "Demo action recorded."}, status=status.HTTP_201_CREATED)
