from django.urls import path

from .views import ArtworkDetailView, ArtworkUploadView

urlpatterns = [
    path('upload/', ArtworkUploadView.as_view(), name='artwork-upload'),
    path('<int:pk>/', ArtworkDetailView.as_view(), name='artwork-detail'),
]
