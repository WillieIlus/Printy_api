"""Legacy subscription callback handler logic."""
from __future__ import annotations

from datetime import date, timedelta
from subscriptions.models import MpesaStkRequest, Payment, Subscription


def _complete_legacy_subscription(stk_req: MpesaStkRequest) -> None:
    """Post-payment logic for legacy subscriptions."""
    sub, _ = Subscription.objects.get_or_create(
        shop=stk_req.shop,
        defaults={"status": Subscription.TRIAL},
    )
    plan = stk_req.plan
    today = date.today()
    period_end = today + timedelta(days=plan.days_in_period())

    sub.plan = plan
    sub.status = Subscription.ACTIVE
    sub.period_start = today
    sub.period_end = period_end
    sub.next_billing_date = period_end
    sub.last_payment_date = today
    sub.save()

    Payment.objects.get_or_create(
        subscription=sub,
        request_id=stk_req.checkout_request_id,
        defaults={
            "amount": stk_req.amount,
            "method": Payment.MPESA_C2B,
            "status": Payment.COMPLETED,
            "receipt_number": stk_req.receipt_number,
            "phone": stk_req.phone,
            "period_start": today,
            "period_end": period_end,
            "metadata": {"stk_request_id": stk_req.id},
        },
    )


def handle_subscription_mpesa_callback(payload: dict) -> dict[str, object]:
    """Deprecated: Use billing.services.callbacks.handle_mpesa_callback instead."""
    from billing.services.callbacks import handle_mpesa_callback
    return handle_mpesa_callback(payload)
