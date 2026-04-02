"""Firebase ID token verification for FastAPI endpoints."""

import logging
import os

import firebase_admin
from firebase_admin import auth as firebase_auth
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_DEV_AUTH_EMAIL = os.environ.get("DEV_AUTH_EMAIL")

if not _DEV_AUTH_EMAIL and not firebase_admin._apps:
    firebase_admin.initialize_app()


def get_verified_user(request: Request) -> dict:
    """FastAPI dependency that verifies a Firebase ID token.

    Reads the Authorization header, verifies the token with Firebase Admin SDK,
    and returns the decoded token dict (contains 'email', 'uid', etc.).
    Raises 401 if the header is missing/malformed or the token is invalid.

    When DEV_AUTH_EMAIL is set, skips token verification entirely and returns
    a synthetic token for that email. Never set this in production.
    """
    if _DEV_AUTH_EMAIL:
        email = request.headers.get("X-Test-Email", _DEV_AUTH_EMAIL)
        return {"email": email, "uid": "dev-local"}

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
    return decoded
