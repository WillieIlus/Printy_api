"""Billing URL configuration."""
from django.urls import path

from billing import views

urlpatterns = [
    # Plans (public)
    path("plans/", views.PlanListView.as_view(), name="billing-plans"),

    # Subscription
    path("subscription/", views.SubscriptionView.as_view(), name="billing-subscription"),
    path("usage/", views.UsageView.as_view(), name="billing-usage"),

    # Lifecycle actions
    path("subscribe/", views.SubscribeView.as_view(), name="billing-subscribe"),
    path("upgrade/", views.UpgradeView.as_view(), name="billing-upgrade"),
    path("downgrade/", views.DowngradeView.as_view(), name="billing-downgrade"),
    path("cancel/", views.CancelView.as_view(), name="billing-cancel"),
    path("reactivate/", views.ReactivateView.as_view(), name="billing-reactivate"),
    path("initiate-renewal/", views.InitiateRenewalView.as_view(), name="billing-initiate-renewal"),
    path("mpesa/test-stk/", views.MpesaSandboxTestStkView.as_view(), name="billing-mpesa-test-stk"),

    # M-Pesa callback (Daraja calls this - no auth)
    path("mpesa/callback/", views.MpesaCallbackView.as_view(), name="billing-mpesa-callback"),

    # Payment history
    path("payments/", views.PaymentListView.as_view(), name="billing-payments"),
    path("payments/<int:pk>/", views.PaymentDetailView.as_view(), name="billing-payment-detail"),
    path("payments/<int:pk>/reconcile/", views.PaymentReconcileView.as_view(), name="billing-payment-reconcile"),

    # Admin / support
    path("admin/manual-activate/", views.AdminManualActivateView.as_view(), name="billing-admin-activate"),
    path("admin/manual-suspend/", views.AdminManualSuspendView.as_view(), name="billing-admin-suspend"),
]
