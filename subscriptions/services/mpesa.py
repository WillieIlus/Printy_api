"""Legacy subscription STK wrapper backed by the shared billing M-Pesa client."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import requests

from billing.services.payments import (
    _build_stk_password,
    _get_mpesa_config,
    _get_mpesa_timeout,
    _safe_json_response,
    get_mpesa_token,
    normalize_phone_number,
)


def normalize_phone(phone: str) -> str:
    return normalize_phone_number(phone)


def get_access_token() -> str:
    return get_mpesa_token()


def initiate_stk_push(
    phone: str,
    amount: Decimal | float,
    account_ref: str,
    description: str = "Printy subscription",
) -> dict[str, Any]:
    config = _get_mpesa_config()
    token = get_mpesa_token()
    password, timestamp = _build_stk_password(config["shortcode"], config["passkey"])
    normalized_phone = normalize_phone_number(phone)
    payload = {
        "BusinessShortCode": config["shortcode"],
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(Decimal(str(amount))),
        "PartyA": normalized_phone,
        "PartyB": config["shortcode"],
        "PhoneNumber": normalized_phone,
        "CallBackURL": config["callback_url"],
        "AccountReference": account_ref[:12],
        "TransactionDesc": description[:255],
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{config['base_url']}/mpesa/stkpush/v1/processrequest"
    resp = requests.post(url, json=payload, headers=headers, timeout=_get_mpesa_timeout())
    resp.raise_for_status()
    return _safe_json_response(resp)
