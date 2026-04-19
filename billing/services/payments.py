"""M-Pesa payment service for STK Push, status reconciliation, and callback parsing."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from billing.models import PaymentTransaction

logger = logging.getLogger("payments")

MPESA_TOKEN_CACHE_KEY = "billing_mpesa_access_token"
MPESA_TOKEN_TTL_SECONDS = 3500
LOCAL_CALLBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}
CANCELLED_RESULT_CODES = {"1032"}
TIMED_OUT_RESULT_CODES = {"1037", "1019", "1025"}


def normalize_phone_number(raw: str) -> str:
    """Convert Kenyan mobile numbers to 2547XXXXXXXX / 2541XXXXXXXX."""
    digits = re.sub(r"\D", "", (raw or "").strip())
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits.startswith(("7", "1")):
        digits = "254" + digits
    if not re.fullmatch(r"254(?:7\d{8}|1\d{8})", digits):
        raise ValueError(f"Invalid Kenyan phone number: {raw!r}")
    return digits


def _get_mpesa_timeout() -> int:
    return int(getattr(settings, "MPESA_TIMEOUT_SECONDS", 30))


def _get_mpesa_config() -> dict[str, str]:
    callback_url = (getattr(settings, "MPESA_CALLBACK_URL", "") or "").strip()
    base_url = (getattr(settings, "MPESA_BASE_URL", "") or "").rstrip("/")
    consumer_key = (getattr(settings, "MPESA_CONSUMER_KEY", "") or "").strip()
    consumer_secret = (getattr(settings, "MPESA_CONSUMER_SECRET", "") or "").strip()
    shortcode = str(getattr(settings, "MPESA_SHORTCODE", "") or "").strip()
    passkey = (getattr(settings, "MPESA_PASSKEY", "") or "").strip()
    env_name = (getattr(settings, "MPESA_ENV", "sandbox") or "sandbox").strip().lower()

    missing = [
        name for name, value in [
            ("MPESA_CONSUMER_KEY", consumer_key),
            ("MPESA_CONSUMER_SECRET", consumer_secret),
            ("MPESA_SHORTCODE", shortcode),
            ("MPESA_PASSKEY", passkey),
            ("MPESA_CALLBACK_URL", callback_url),
        ] if not value
    ]
    if missing:
        raise ValueError(f"Missing M-Pesa configuration: {', '.join(missing)}")

    parsed_callback = urlparse(callback_url)
    if not parsed_callback.scheme or not parsed_callback.netloc:
        raise ValueError("MPESA_CALLBACK_URL must be an absolute URL.")

    if env_name == "production":
        if parsed_callback.scheme != "https":
            raise ValueError("MPESA_CALLBACK_URL must use HTTPS in production.")
        if (parsed_callback.hostname or "").lower() in LOCAL_CALLBACK_HOSTS:
            raise ValueError("MPESA_CALLBACK_URL cannot point to localhost in production.")
    elif (parsed_callback.hostname or "").lower() in LOCAL_CALLBACK_HOSTS:
        logger.warning(
            "MPESA_CALLBACK_URL points to localhost; sandbox callbacks will not reach this server without a public tunnel."
        )

    return {
        "base_url": base_url,
        "callback_url": callback_url,
        "consumer_key": consumer_key,
        "consumer_secret": consumer_secret,
        "shortcode": shortcode,
        "passkey": passkey,
        "env_name": env_name,
    }


def _safe_json_response(resp: requests.Response | None) -> dict[str, Any]:
    if resp is None:
        return {}
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except ValueError:
        return {"raw_text": resp.text[:1000]}


def _fetch_mpesa_token() -> str:
    config = _get_mpesa_config()
    auth_string = base64.b64encode(
        f"{config['consumer_key']}:{config['consumer_secret']}".encode()
    ).decode()
    url = f"{config['base_url']}/oauth/v1/generate?grant_type=client_credentials"
    headers = {"Authorization": f"Basic {auth_string}"}

    resp = requests.get(url, headers=headers, timeout=_get_mpesa_timeout())
    resp.raise_for_status()
    data = _safe_json_response(resp)
    token = data.get("access_token")
    if not token:
        raise ValueError("Daraja token response did not contain access_token.")
    return token


def get_mpesa_token() -> str:
    token = cache.get(MPESA_TOKEN_CACHE_KEY)
    if token:
        return token
    token = _fetch_mpesa_token()
    cache.set(MPESA_TOKEN_CACHE_KEY, token, MPESA_TOKEN_TTL_SECONDS)
    return token


def build_idempotency_key(owner_id: int, plan_code: str, txn_type: str, nonce: str | None = None) -> str:
    nonce = nonce or uuid.uuid4().hex
    raw = f"{owner_id}:{plan_code}:{txn_type}:{nonce}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def generate_account_reference(owner_email: str, plan_code: str) -> str:
    base_reference = str(
        getattr(settings, "MPESA_ACCOUNT_REFERENCE_DEFAULT", "PRINTY") or "PRINTY"
    ).upper()
    prefix = re.sub(r"[^A-Z0-9]", "", owner_email.upper().split("@")[0])[:4]
    plan_hint = re.sub(r"[^A-Z0-9]", "", plan_code.upper())[:4]
    return f"{base_reference[:4]}{prefix}{plan_hint}"[:12] or "PRINTY"


def _build_transaction_description(plan_name: str) -> str:
    default_desc = str(
        getattr(settings, "MPESA_TRANSACTION_DESC_DEFAULT", "Printy payment") or "Printy payment"
    ).strip()
    return f"{default_desc} {plan_name}"[:255].strip()


def _build_stk_password(shortcode: str, passkey: str) -> tuple[str, str]:
    timestamp = timezone.localtime(timezone.now()).strftime("%Y%m%d%H%M%S")
    raw = f"{shortcode}{passkey}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def _map_result_code_to_status(result_code: str) -> str:
    if result_code == "0":
        return PaymentTransaction.STATUS_SUCCESS
    if result_code in CANCELLED_RESULT_CODES:
        return PaymentTransaction.STATUS_CANCELLED
    if result_code in TIMED_OUT_RESULT_CODES:
        return PaymentTransaction.STATUS_TIMED_OUT
    return PaymentTransaction.STATUS_FAILED


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
    phone_normalized = normalize_phone_number(phone_number)
    config = _get_mpesa_config()
    amount_decimal = Decimal(str(amount))
    if amount_decimal <= 0:
        raise ValueError("STK push amount must be greater than zero.")

    account_ref = generate_account_reference(owner.email, plan.code)
    idem_key = idempotency_key or build_idempotency_key(owner.id, plan.code, transaction_type)
    desc = _build_transaction_description(plan.name)

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
        amount=amount_decimal,
        currency="KES",
        status=PaymentTransaction.STATUS_PENDING,
        idempotency_key=idem_key,
        initiated_at=timezone.now(),
        external_reference=account_ref,
    )

    payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] = {}

    try:
        token = get_mpesa_token()
        password, timestamp = _build_stk_password(config["shortcode"], config["passkey"])
        payload = {
            "BusinessShortCode": config["shortcode"],
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(amount_decimal),
            "PartyA": phone_normalized,
            "PartyB": config["shortcode"],
            "PhoneNumber": phone_normalized,
            "CallBackURL": config["callback_url"],
            "AccountReference": account_ref,
            "TransactionDesc": desc,
        }
        txn.raw_request = payload
        txn.save(update_fields=["raw_request", "updated_at"])

        url = f"{config['base_url']}/mpesa/stkpush/v1/processrequest"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=_get_mpesa_timeout())
        response_payload = _safe_json_response(resp)
        resp.raise_for_status()

        txn.merchant_request_id = str(response_payload.get("MerchantRequestID", "") or "")
        txn.checkout_request_id = str(response_payload.get("CheckoutRequestID", "") or "")
        txn.raw_response = response_payload
        txn.response_code = str(response_payload.get("ResponseCode", "") or "")
        txn.response_description = str(response_payload.get("ResponseDescription", "") or "")[:255]
        txn.customer_message = str(response_payload.get("CustomerMessage", "") or "")[:255]

        if txn.response_code == "0" and txn.checkout_request_id:
            txn.status = PaymentTransaction.STATUS_PROCESSING
        else:
            txn.status = PaymentTransaction.STATUS_FAILED
            txn.result_desc = (
                txn.response_description or txn.customer_message or "Daraja rejected the STK push request."
            )[:255]
            txn.completed_at = timezone.now()

        txn.save(update_fields=[
            "merchant_request_id",
            "checkout_request_id",
            "raw_response",
            "response_code",
            "response_description",
            "customer_message",
            "status",
            "result_desc",
            "completed_at",
            "updated_at",
        ])
        logger.info(
            "STK push initiated for owner=%s checkout_request_id=%s status=%s",
            owner.email,
            txn.checkout_request_id,
            txn.status,
        )
    except Exception as exc:
        txn.raw_request = payload
        if response_payload:
            txn.raw_response = response_payload
        txn.status = PaymentTransaction.STATUS_FAILED
        txn.result_desc = str(exc)[:255]
        txn.completed_at = timezone.now()
        txn.save(update_fields=[
            "raw_request",
            "raw_response",
            "status",
            "result_desc",
            "completed_at",
            "updated_at",
        ])
        logger.exception("STK push failed for owner=%s: %s", owner.email, exc)

    return txn


def query_transaction_status(checkout_request_id: str) -> dict[str, Any]:
    config = _get_mpesa_config()
    token = get_mpesa_token()
    password, timestamp = _build_stk_password(config["shortcode"], config["passkey"])
    payload = {
        "BusinessShortCode": config["shortcode"],
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }
    url = f"{config['base_url']}/mpesa/stkpushquery/v1/query"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resp = requests.post(url, json=payload, headers=headers, timeout=_get_mpesa_timeout())
    resp.raise_for_status()
    return _safe_json_response(resp)


def reconcile_transaction(txn: PaymentTransaction) -> dict[str, Any]:
    if not txn.checkout_request_id:
        raise ValueError("Transaction has no checkout_request_id to query.")

    response_payload = query_transaction_status(txn.checkout_request_id)
    txn.raw_response = {
        **(txn.raw_response or {}),
        "stk_query": response_payload,
    }
    txn.response_code = str(response_payload.get("ResponseCode", txn.response_code) or "")
    txn.response_description = str(
        response_payload.get("ResponseDescription", txn.response_description) or ""
    )[:255]

    result_code = response_payload.get("ResultCode")
    result_desc = response_payload.get("ResultDesc")
    if result_code is not None:
        result_code_str = str(result_code)
        txn.result_code = result_code_str
        txn.result_desc = str(result_desc or "")[:255]
        if result_code_str != "0":
            txn.status = _map_result_code_to_status(result_code_str)
            txn.completed_at = timezone.now()
        elif txn.status == PaymentTransaction.STATUS_PENDING:
            txn.status = PaymentTransaction.STATUS_PROCESSING

    txn.save(update_fields=[
        "raw_response",
        "response_code",
        "response_description",
        "result_code",
        "result_desc",
        "status",
        "completed_at",
        "updated_at",
    ])
    return response_payload


def verify_callback_minimally(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    stk = payload.get("Body", {}).get("stkCallback")
    if not isinstance(stk, dict):
        return False
    return bool(stk.get("CheckoutRequestID") or stk.get("MerchantRequestID"))


def parse_callback(payload: dict) -> dict[str, Any]:
    stk = payload.get("Body", {}).get("stkCallback", {})
    result_code = str(stk.get("ResultCode", "999"))
    result_desc = str(stk.get("ResultDesc", "") or "")
    checkout_request_id = str(stk.get("CheckoutRequestID", "") or "")
    merchant_request_id = str(stk.get("MerchantRequestID", "") or "")
    success = result_code == "0"

    receipt = None
    amount = None
    phone = None
    txn_date = None

    items = stk.get("CallbackMetadata", {}).get("Item", []) if success else []
    item_map: dict[str, Any] = {}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("Name")
            if not name:
                continue
            item_map[str(name)] = item.get("Value")

    receipt_value = item_map.get("MpesaReceiptNumber")
    if receipt_value is not None:
        receipt = str(receipt_value)
    raw_amount = item_map.get("Amount")
    if raw_amount is not None:
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, TypeError, ValueError):
            amount = None
    phone_value = item_map.get("PhoneNumber")
    if phone_value is not None:
        phone = str(phone_value)
    txn_date_value = item_map.get("TransactionDate")
    if txn_date_value is not None:
        txn_date = str(txn_date_value)

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


def record_callback(txn: PaymentTransaction, parsed: dict[str, Any], raw_payload: dict) -> PaymentTransaction:
    now = timezone.now()
    txn.raw_callback = raw_payload
    txn.callback_received_at = now
    txn.result_code = parsed["result_code"]
    txn.result_desc = parsed["result_desc"][:255]

    status_value = _map_result_code_to_status(parsed["result_code"])
    txn.status = status_value
    if parsed.get("mpesa_receipt_number"):
        txn.mpesa_receipt_number = parsed["mpesa_receipt_number"][:50]
    if parsed.get("phone_number"):
        txn.phone_number = parsed["phone_number"][:20]
    if status_value in {
        PaymentTransaction.STATUS_SUCCESS,
        PaymentTransaction.STATUS_FAILED,
        PaymentTransaction.STATUS_CANCELLED,
        PaymentTransaction.STATUS_TIMED_OUT,
    }:
        txn.completed_at = now

    txn.save(update_fields=[
        "raw_callback",
        "callback_received_at",
        "result_code",
        "result_desc",
        "status",
        "mpesa_receipt_number",
        "phone_number",
        "completed_at",
        "updated_at",
    ])
    return txn
