from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from .models import BetaFeedback
from .serializers import BetaFeedbackSerializer
from .throttling import BetaFeedbackThrottle


class BetaFeedbackViewSet(GenericViewSet):
    queryset = BetaFeedback.objects.all()
    serializer_class = BetaFeedbackSerializer
    permission_classes = [IsAuthenticated]
    throttle_classes = [BetaFeedbackThrottle]

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        BetaFeedback.objects.create(
            user=request.user,
            **serializer.validated_data,
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="mine")
    def mine(self, request):
        qs = BetaFeedback.objects.filter(user=request.user).order_by("-created_at")
        serializer = BetaFeedbackSerializer(qs, many=True)
        return Response(serializer.data)
