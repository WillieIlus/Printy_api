from django.shortcuts import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from services.public_matching import (
    get_booklet_marketplace_matches,
    get_marketplace_matches,
    get_shop_specific_preview,
    recompute_shop_match_readiness,
)
from shops.models import Shop

from .public_matching_serializers import (
    PublicBookletMatchPayloadSerializer,
    PublicCalculatorPayloadSerializer,
    PublicCalculatorResponseSerializer,
)


class PublicMatchShopsView(APIView):
    """
    Public endpoint for marketplace shop matching and preview.
    Sample payload:
    {
        "job_type": "business_cards",
        "quantity": 100,
        "width_mm": 85,
        "height_mm": 55,
        "paper_preference": "300gsm matt",
        "print_sides": "SIMPLEX",
        "location_slug": "westlands"
    }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PublicCalculatorPayloadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        response = get_marketplace_matches(serializer.validated_data)
        return Response(PublicCalculatorResponseSerializer(response).data)


class PublicMatchBookletShopsView(APIView):
    """Job-first booklet matching — accepts booklet spec, returns best matching shops."""
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PublicBookletMatchPayloadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        response = get_booklet_marketplace_matches(serializer.validated_data)
        return Response(PublicCalculatorResponseSerializer(response).data)


class PublicShopCalculatorPreviewView(APIView):
    """
    Public endpoint for a single shop's calculator preview.
    Sample payload:
    {
        "quantity": 500,
        "width_mm": 210,
        "height_mm": 297,
        "paper_type": "art",
        "paper_gsm": 150
    }
    """
    permission_classes = [AllowAny]

    def post(self, request, slug):
        shop = get_object_or_404(Shop, slug=slug, is_active=True, is_public=True)
        recompute_shop_match_readiness(shop)
        serializer = PublicCalculatorPayloadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        response = get_shop_specific_preview(shop, serializer.validated_data)
        return Response(PublicCalculatorResponseSerializer(response).data)
