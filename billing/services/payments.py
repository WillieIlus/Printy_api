"""M-Pesa payment service — STK Push, token caching, callback normalization."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from billing.models import PaymentTransaction

logger = logging.getLogger("payments")

MPESA_TOKEN_CACHE_KEY = "billing_mpesa_access_token"
MPESA_TOKEN_TTL_SECONDS = 3500  # slightly less than 3600 to avoid expiry edge cases


# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------

def normalize_phone_number(raw: str) -> str:
    """Convert any Kenyan phone format to 2547XXXXXXXX (E.164 without +)."""
    digits = re.sub(r"\D", "", raw.strip())
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif digits.startswith("7") and len(digits) == 9:
        digits = "254" + digits
    elif digits.startswith("+254"):
        digits = digits[1:]
    if not re.match(r"^2547\d{8}$", digits):
        raise ValueError(f"Invalid Kenyan phone number: {raw!r} → {digits!r}")
    return digits


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _fetch_mpesa_token() -> str:
    consumer_key = settings.MPESA_CONSUMER_KEY
    consumer_secret = settings.MPESA_CONSUMER_SECRET
    auth_string = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()

    url = f"{settings.MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials"
    headers = {"Authorization": f"Basic {auth_string}"}
    timeout = getattr(settings, "MPESA_TIMEOUT_SECONDS", 30)

    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError(f"No access_token in Daraja response: {data}")
    return token


def get_mpesa_token() -> str:
    """Return a cached OAuth token, refreshing when expired."""
    token = cache.get(MPESA_TOKEN_CACHE_KEY)
    if not token:
        token = _fetch_mpesa_token()
        cache.set(MPESA_TOKEN_CACHE_KEY, token, MPESA_TOKEN_TTL_SECONDS)
    return token


# ---------------------------------------------------------------------------
# Idempotency & reference helpers
# ---------------------------------------------------------------------------

def build_idempotency_key(owner_id: int, plan_code: str, txn_type: str, nonce: str | None = None) -> str:
    nonce = nonce or uuid.uuid4().hex
    raw = f"{owner_id}:{plan_code}:{txn_type}:{nonce}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def generate_account_reference(owner_email: str, plan_code: str) -> str:
    """Short human-readable reference — max 12 chars for Daraja."""
    prefix = re.sub(r"[^A-Z0-9]", "", owner_email.upper().split("@")[0])[:5]
    return f"PRINTY-{prefix}"[:12]


# ---------------------------------------------------------------------------
# STK Push
# ---------------------------------------------------------------------------

def _build_stk_password() -> tuple[str, str]:
    """Return (password_b64, timestamp_str) for Daraja LipaNaMpesa Online."""
    shortcode = settings.MPESA_SHORTCODE
    passkey = settings.MPESA_PASSKEY
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{shortcode}{passkey}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def initiate_stk_push(
    *,
    owner,
    subscription,
    plan,
    phone_number: str,
    amount: Decimal,
    transaction_type: str,
    idempotency_key: str | None = None,
) -> PaymentTransaction:
    """
    Create a PaymentTransaction record then fire the Daraja STK push.
    Returns the transaction regardless of Daraja outcome — callers check txn.status.
    """
    phone_normalized = normalize_phone_number(phone_number)
    account_ref = generate_account_reference(owner.email, plan.code)
    idem_key = idempotency_key or build_idempotency_key(owner.id, plan.code, transaction_type)
    desc = f"Printy.ke {plan.name} subscription"

    txn = PaymentTransaction.objects.create(
        subscription=subscription,
        owner=owner,
        plan=plan,
        transaction_type=transaction_type,
        provider=PaymentTransaction.PROVIDER_MPESA,
        provider_method=PaymentTransaction.METHOD_STK,
        phone_number=phone_normalized,
        account_reference=account_ref,
        transaction_desc=desc,
        amount=amount,
        currency="KES",
        status=PaymentTransaction.STATUS_PENDING,
        idempotency_key=idem_key,
        initiated_at=timezone.now(),
    )

    try:
        token = get_mpesa_token()
        password, timestamp = _build_stk_password()
        callback_url = getattr(settings, "MPESA_CALLBACK_URL", settings.MPESA_STK_CALLBACK_URL)

        payload = {
            "BusinessShortCode": settings.MPESA_SHORTCODE,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(amount),  # Daraja expects integer KES
            "PartyA": phone_normalized,
            "PartyB": settings.MPESA_SHORTCODE,
            "PhoneNumber": phone_normalized,
            "CallBackURL": callback_url,
            "AccountReference": account_ref,
            "TransactionDesc": desc,
        }

        timeout = getattr(settings, "MPESA_TIMEOUT_SECONDS", 30)
        url = f"{settings.MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        resp_data = resp.json()

        merchant_request_id = resp_data.get("MerchantRequestID", "")
        checkout_request_id = resp_data.get("CheckoutRequestID", "")

        txn.merchant_request_id = merchant_request_id
        txn.checkout_request_id = checkout_request_id
        txn.raw_request = resp_data
        txn.status = PaymentTransaction.STATUS_PROCESSING
        txn.save(update_fields=[
            "merchant_request_id", "checkout_request_id", "raw_request", "status", "updated_at"
        ])
        logger.info("STK push initiated: checkout_request_id=%s owner=%s", checkout_request_id, owner.email)

    except Exception as exc:
        txn.status = PaymentTransaction.STATUS_FAILED
        txn.result_desc = str(exc)[:255]
        txn.completed_at = timezone.now()
        txn.save(update_fields=["status", "result_desc", "completed_at", "updated_at"])
        logger.exception("STK push failed for owner=%s: %s", owner.email, exc)

    return txn


def query_transaction_status(checkout_request_id: str) -> dict:
    """Query Daraja for current transaction status."""
    token = get_mpesa_token()
    password, timestamp = _build_stk_password()

    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }
    url = f"{settings.MPESA_BASE_URL}/mpesa/stkpushquery/v1/query"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = getattr(settings, "MPESA_TIMEOUT_SECONDS", 30)

    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Callback parsing
# ---------------------------------------------------------------------------

def verify_callback_minimally(payload: dict) -> bool:
    """Light structural check — full HMAC verification not available on Daraja sandbox."""
    return (
        isinstance(payload, dict)
        and "Body" in payload
        and "stkCallback" in payload.get("Body", {})
    )


def parse_callback(payload: dict) -> dict:
    """
    Normalise a Daraja STK callback into a flat dict.

    Returns:
        {
            "checkout_request_id": str,
            "merchant_request_id": str,
            "result_code": str,
            "result_desc": str,
            "mpesa_receipt_number": str | None,
            "amount": Decimal | None,
            "phone_number": str | None,
            "transaction_date": str | None,
            "success": bool,
        }
    """
    stk = payload.get("Body", {}).get("stkCallback", {})
    result_code = str(stk.get("ResultCode", "999"))
    result_desc = stk.get("ResultDesc", "")
    checkout_request_id = stk.get("CheckoutRequestID", "")
    merchant_request_id = stk.get("MerchantRequestID", "")
    success = result_code == "0"

    receipt = None
    amount = None
    phone = None
    txn_date = None

    if success:
        items = stk.get("CallbackMetadata", {}).get("Item", [])
        item_map = {i["Name"]: i.get("Value") for i in items}
        receipt = item_map.get("MpesaReceiptNumber")
        raw_amount = item_map.get("Amount")
        amount = Decimal(str(raw_amount)) if raw_amount is not None else None
        phone = str(item_map.get("PhoneNumber", ""))
        txn_date = str(item_map.get("TransactionDate", ""))

    return {
        "checkout_request_id": checkout_request_id,
        "merchant_request_id": merchant_request_id,
        "result_code": result_code,
        "result_desc": result_desc,
        "mpesa_receipt_number": receipt,
        "amount": amount,
        "phone_number": phone,
        "transaction_date": txn_date,
        "success": success,
    }


def record_callback(txn: PaymentTransaction, parsed: dict, raw_payload: dict) -> PaymentTransaction:
    """Persist callback data onto the transaction.  Does NOT activate subscription."""
    now = timezone.now()
    txn.raw_callback = raw_payload
    txn.callback_received_at = now
    txn.result_code = parsed["result_code"]
    txn.result_desc = parsed["result_desc"]

    if parsed["success"]:
        txn.status = PaymentTransaction.STATUS_SUCCESS
        txn.mpesa_receipt_number = parsed.get("mpesa_receipt_number") or ""
        txn.completed_at = now
    else:
        txn.status = PaymentTransaction.STATUS_FAILED
        txn.completed_at = now

    txn.save(update_fields=[
        "raw_callback", "callback_received_at", "result_code", "result_desc",
        "status", "mpesa_receipt_number", "completed_at", "updated_at",
    ])
    return txn
