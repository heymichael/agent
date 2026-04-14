"""Shared Bill.com v3 authentication helper."""

import os

import requests

from .credentials import load_json_credential


def billcom_login() -> tuple[str, str, dict]:
    """Login to Bill.com and return (base_url, session_id, headers).

    Reads credentials from the file path in VENDOR_BILL_CREDENTIALS
    (JSON with keys: userName, password, orgId, devKey, optional baseUrl).
    """
    creds = load_json_credential("VENDOR_BILL_CREDENTIALS")
    base = creds.get("baseUrl", "https://gateway.prod.bill.com/connect")

    resp = requests.post(
        f"{base}/v3/login",
        json={
            "username": creds["userName"],
            "password": creds["password"],
            "organizationId": creds["orgId"],
            "devKey": creds["devKey"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    session_id = resp.json()["sessionId"]

    headers = {
        "devKey": creds["devKey"],
        "sessionId": session_id,
        "Accept": "application/json",
    }
    return base, session_id, headers
