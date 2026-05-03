from django.urls import path
from .views import ApplyView, DemoActionView, SpotsView

urlpatterns = [
    path("spots/", SpotsView.as_view(), name="early-access-spots"),
    path("apply/", ApplyView.as_view(), name="early-access-apply"),
    path("demo-actions/", DemoActionView.as_view(), name="demo-actions"),
]
