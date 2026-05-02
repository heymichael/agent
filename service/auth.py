"""Firebase ID token verification for FastAPI endpoints."""

import logging
import os
from typing import Callable
from urllib.request import urlopen

import firebase_admin
from firebase_admin import auth as firebase_auth
from fastapi import Depends, HTTPException, Request

logger = logging.getLogger(__name__)

_DEV_AUTH_EMAIL = os.environ.get("DEV_AUTH_EMAIL")
_FIREBASE_CERT_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/"
    "securetoken@system.gserviceaccount.com"
)

# Header sent by the frontend `agentFetch` wrapper to pin a request to a
# specific org. Falls back to the `active_org` Firebase custom claim if the
# header is absent (Firebase doesn't carry it today; future-proofing).
_ACTIVE_ORG_HEADER = "X-Active-Org"
_ACTIVE_ORG_CLAIM = "active_org"

if not _DEV_AUTH_EMAIL and not firebase_admin._apps:
    firebase_admin.initialize_app()


def warm_firebase_public_keys(timeout: float = 5.0) -> None:
    """Prime the network path Firebase token verification depends on."""
    if _DEV_AUTH_EMAIL:
        return
    with urlopen(_FIREBASE_CERT_URL, timeout=timeout) as response:
        response.read()


def _resolve_active_org_slug(
    email: str,
    requested_slug: str | None,
) -> str | None:
    """Resolve `caller["active_org_slug"]` from the request and the user's memberships.

    Strategy 197-r2 / task 254 phase 2 rules:
    - If `requested_slug` is supplied and the user has a membership in it,
      return it.
    - If `requested_slug` is supplied but the user is NOT a member, raise
      403 (`Active-Org-Forbidden`). Membership enforcement (gate 3 of the
      request flow) lives here so downstream code can trust the slug.
    - If `requested_slug` is missing and the user has exactly one
      membership, default to that slug.
    - If `requested_slug` is missing and the user has more than one
      membership, raise 400 (`Active-Org-Required`) — the frontend must
      prompt for an org before retrying.
    - If `requested_slug` is missing and the user has zero memberships,
      return None. Phase 2 does not enforce data scoping, so endpoints
      that need an org will reject downstream; endpoints that don't (like
      `/me`) still respond.
    """
    # Defer the DB import so service.auth stays importable in environments
    # where psycopg/pool isn't initialized (e.g. some unit-test fixtures).
    from . import pg_client

    memberships = pg_client.list_user_org_slugs(email)

    if requested_slug:
        if requested_slug in memberships:
            return requested_slug
        raise HTTPException(
            status_code=403,
            detail={
                "code": "Active-Org-Forbidden",
                "message": (
                    f"User is not a member of org '{requested_slug}'."
                ),
            },
        )

    if len(memberships) == 1:
        return memberships[0]
    if len(memberships) == 0:
        return None
    raise HTTPException(
        status_code=400,
        detail={
            "code": "Active-Org-Required",
            "message": (
                "User has multiple org memberships; "
                f"send the X-Active-Org header. memberships={memberships}"
            ),
        },
    )


def get_verified_user(request: Request) -> dict:
    """FastAPI dependency that verifies a Firebase ID token and resolves active org.

    Reads the Authorization header, verifies the token with Firebase Admin SDK,
    and returns the decoded token dict (contains 'email', 'uid', etc.) augmented
    with `active_org_slug` resolved from the `X-Active-Org` header (or the
    `active_org` Firebase claim, if ever present). Raises 401 on missing/invalid
    token, 400 (`Active-Org-Required`) when a multi-membership caller omits the
    header, or 403 (`Active-Org-Forbidden`) when the caller asks for an org they
    don't belong to.

    When DEV_AUTH_EMAIL is set, skips token verification entirely and returns
    a synthetic token for that email. Active-org resolution still runs against
    the local DB so dev parity is preserved. Never set DEV_AUTH_EMAIL in
    production.
    """
    if _DEV_AUTH_EMAIL:
        email = request.headers.get("X-Test-Email", _DEV_AUTH_EMAIL)
        requested = request.headers.get(_ACTIVE_ORG_HEADER)
        active_slug = _resolve_active_org_slug(email, requested)
        return {
            "email": email,
            "uid": "dev-local",
            "active_org_slug": active_slug,
        }

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
        )
    token = header.removeprefix("Bearer ")
    try:
        decoded = firebase_auth.verify_id_token(token)
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except firebase_auth.InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as exc:
        logger.warning("Token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Token verification failed")

    email = decoded.get("email", "")
    requested = (
        request.headers.get(_ACTIVE_ORG_HEADER)
        or decoded.get(_ACTIVE_ORG_CLAIM)
    )
    decoded["active_org_slug"] = _resolve_active_org_slug(email, requested)
    decoded["_raw_token"] = token
    return decoded


def get_caller_enabled_apps(
    caller: dict = Depends(get_verified_user),
) -> list[str]:
    """Return the active org's `enabled_apps` for the current request.

    Phase 4 of multi-org tenancy (task 254). FastAPI's per-request
    dependency cache means this resolves at most once per request even
    if multiple `require_app(...)` dependencies reference it. Endpoints
    that don't use `require_app` never trigger this lookup.

    Returns `[]` if the caller has no active org (zero-membership user
    or an endpoint that didn't enforce a slug). The downstream
    `require_app` distinguishes "no active org" (400) from "app not
    enabled" (403) so the 403 path is reserved for genuine entitlement
    misses.
    """
    from . import pg_client

    slug = caller.get("active_org_slug")
    if not slug:
        return []
    return pg_client.get_org_enabled_apps(slug)


def require_app(app_slug: str) -> Callable[..., str]:
    """Build a FastAPI dependency that enforces app entitlement.

    Returns the active org slug on success so endpoints can drop their
    own `_require_active_org(caller)` call when they use this dep.

    Failure modes:
    - No active org resolved (zero-membership user, or somehow no slug
      pinned to the request) → 400 `Active-Org-Required`. Mirrors the
      existing helper in `service/app.py` so the frontend treats both
      paths identically.
    - Active org doesn't have `app_slug` in its `enabled_apps` →
      403 `App-Not-Enabled`. The 403 message names both the app and the
      org so logs are immediately diagnosable.

    The role-based gate (`app_granting_roles` / `_get_caller_roles`)
    stays as-is; entitlement is layered on top, not a replacement
    (197-r2 / task 254 plan §"App entitlement is layered, not
    replacing").
    """
    def dep(
        caller: dict = Depends(get_verified_user),
        enabled_apps: list[str] = Depends(get_caller_enabled_apps),
    ) -> str:
        slug = caller.get("active_org_slug")
        if not slug:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "Active-Org-Required",
                    "message": "No active org resolved for this caller.",
                },
            )
        if app_slug not in enabled_apps:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "App-Not-Enabled",
                    "message": (
                        f"App '{app_slug}' is not enabled for org '{slug}'."
                    ),
                },
            )
        return slug
    return dep
