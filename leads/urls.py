from django.urls import path
from .views import ApplyView, SpotsView

urlpatterns = [
    path("spots/", SpotsView.as_view(), name="early-access-spots"),
    path("apply/", ApplyView.as_view(), name="early-access-apply"),
]
