"""QuickBooks Online OAuth2 helpers — authorization flow and token refresh.

OAuth redirect URIs registered in Intuit Developer Portal:
  - Production: https://haderach.ai/agent/api/qbo/callback
  - Local dev:  http://localhost:8000/qbo/callback

Credentials are stored in VENDOR_QBO_CREDENTIALS env var (JSON):
  {"client_id": "...", "client_secret": "...", "realm_id": "...", "refresh_token": "..."}

After the initial authorization dance populates the refresh_token, the sync
job uses `refresh_access_token()` to get a fresh access token on each run.
QuickBooks rotates the refresh token on every use — the caller is responsible
for persisting the new refresh token (Secret Manager in prod, .env locally).
"""

import json
import logging
import os
from base64 import b64encode
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

INTUIT_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
INTUIT_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPES = "com.intuit.quickbooks.accounting"


def _load_creds() -> dict:
    raw = os.environ.get("VENDOR_QBO_CREDENTIALS", "{}")
    return json.loads(raw)


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    pair = f"{client_id}:{client_secret}"
    return "Basic " + b64encode(pair.encode()).decode()


def get_authorization_url(redirect_uri: str, state: str = "") -> str:
    """Build the Intuit OAuth2 authorization URL the user should be redirected to."""
    creds = _load_creds()
    if not state:
        import secrets
        state = secrets.token_urlsafe(16)
    params = {
        "client_id": creds["client_id"],
        "scope": QBO_SCOPES,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{INTUIT_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for access + refresh tokens.

    Returns the full Intuit token response:
      {"access_token", "refresh_token", "token_type", "expires_in",
       "x_refresh_token_expires_in", ...}
    """
    creds = _load_creds()
    resp = requests.post(
        INTUIT_TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(creds["client_id"], creds["client_secret"]),
            "Accept": "application/json",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token() -> dict:
    """Use the stored refresh token to get a new access + refresh token pair.

    Returns the full Intuit token response. The caller MUST persist the new
    refresh_token — QuickBooks invalidates the old one on each refresh.
    """
    creds = _load_creds()
    refresh_token = creds.get("refresh_token", "")
    if not refresh_token:
        raise RuntimeError(
            "No refresh_token in VENDOR_QBO_CREDENTIALS. "
            "Complete the OAuth authorization flow first via GET /qbo/auth."
        )

    resp = requests.post(
        INTUIT_TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(creds["client_id"], creds["client_secret"]),
            "Accept": "application/json",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token_data = resp.json()
    logger.info(
        "QBO token refreshed (access expires in %ss, refresh expires in %ss)",
        token_data.get("expires_in"),
        token_data.get("x_refresh_token_expires_in"),
    )
    return token_data
