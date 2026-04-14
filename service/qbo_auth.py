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

OAuth endpoints are fetched from the Intuit OpenID discovery document and
cached for 1 hour.  Hardcoded fallbacks are used if the fetch fails.
"""

import logging
import os
import secrets
import time
from base64 import b64encode
from urllib.parse import urlencode

import requests

from .credentials import load_json_credential

logger = logging.getLogger(__name__)

QBO_SCOPES = "com.intuit.quickbooks.accounting"

_DISCOVERY_PROD = "https://developer.api.intuit.com/.well-known/openid_configuration"
_DISCOVERY_SANDBOX = "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration"

_FALLBACK_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
_FALLBACK_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

_discovery_cache: dict = {}
_DISCOVERY_TTL = 3600


class QBOAuthError(RuntimeError):
    """Raised when an Intuit OAuth token request fails with a parseable error."""

    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        super().__init__(message)


def _is_sandbox() -> bool:
    return "sandbox" in os.getenv("QBO_API_BASE_URL", "sandbox").lower()


def _fetch_discovery() -> dict:
    """Fetch and cache the Intuit OpenID discovery document."""
    now = time.monotonic()
    if _discovery_cache.get("data") and now - _discovery_cache.get("ts", 0) < _DISCOVERY_TTL:
        return _discovery_cache["data"]

    url = _DISCOVERY_SANDBOX if _is_sandbox() else _DISCOVERY_PROD
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _discovery_cache["data"] = data
        _discovery_cache["ts"] = now
        logger.info("Intuit discovery document refreshed from %s", url)
        return data
    except Exception:
        logger.warning("Failed to fetch Intuit discovery document from %s, using fallbacks", url)
        return {}


def _get_auth_url() -> str:
    doc = _fetch_discovery()
    return doc.get("authorization_endpoint", _FALLBACK_AUTH_URL)


def _get_token_url() -> str:
    doc = _fetch_discovery()
    return doc.get("token_endpoint", _FALLBACK_TOKEN_URL)


def _load_creds() -> dict:
    return load_json_credential("VENDOR_QBO_CREDENTIALS")


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    pair = f"{client_id}:{client_secret}"
    return "Basic " + b64encode(pair.encode()).decode()


def _raise_for_token_error(resp: requests.Response) -> None:
    """Parse an Intuit token error response and raise QBOAuthError."""
    if resp.status_code < 400:
        return

    error_code = "unknown"
    detail = resp.text
    try:
        body = resp.json()
        error_code = body.get("error", error_code)
        detail = body.get("error_description", detail)
    except Exception:
        pass

    if error_code == "invalid_grant":
        raise QBOAuthError(
            error_code,
            f"Refresh token expired or revoked (HTTP {resp.status_code}). "
            "Re-authorize via GET /qbo/auth.",
        )

    raise QBOAuthError(
        error_code,
        f"Intuit token request failed (HTTP {resp.status_code}): {error_code} — {detail}",
    )


def get_authorization_url(redirect_uri: str, state: str = "") -> tuple[str, str]:
    """Build the Intuit OAuth2 authorization URL the user should be redirected to.

    Returns (url, state) so the caller can persist the state for CSRF validation.
    """
    creds = _load_creds()
    if not state:
        state = secrets.token_urlsafe(16)
    params = {
        "client_id": creds["client_id"],
        "scope": QBO_SCOPES,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{_get_auth_url()}?{urlencode(params)}", state


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for access + refresh tokens.

    Returns the full Intuit token response:
      {"access_token", "refresh_token", "token_type", "expires_in",
       "x_refresh_token_expires_in", ...}

    Raises QBOAuthError for structured Intuit error responses.
    """
    creds = _load_creds()
    resp = requests.post(
        _get_token_url(),
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
    _raise_for_token_error(resp)
    return resp.json()


def refresh_access_token() -> dict:
    """Use the stored refresh token to get a new access + refresh token pair.

    Returns the full Intuit token response. The caller MUST persist the new
    refresh_token — QuickBooks invalidates the old one on each refresh.

    Raises QBOAuthError for expired/revoked tokens and other Intuit errors.
    """
    creds = _load_creds()
    refresh_token = creds.get("refresh_token", "")
    if not refresh_token:
        raise QBOAuthError(
            "missing_refresh_token",
            "No refresh_token in VENDOR_QBO_CREDENTIALS. "
            "Complete the OAuth authorization flow first via GET /qbo/auth.",
        )

    resp = requests.post(
        _get_token_url(),
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
    _raise_for_token_error(resp)
    token_data = resp.json()
    logger.info(
        "QBO token refreshed (access expires in %ss, refresh expires in %ss)",
        token_data.get("expires_in"),
        token_data.get("x_refresh_token_expires_in"),
    )
    return token_data
