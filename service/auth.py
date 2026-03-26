"""Firebase ID token verification for FastAPI endpoints."""

import logging

import firebase_admin
from firebase_admin import auth as firebase_auth
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

if not firebase_admin._apps:
    firebase_admin.initialize_app()


def get_verified_user(request: Request) -> dict:
    """FastAPI dependency that verifies a Firebase ID token.

    Reads the Authorization header, verifies the token with Firebase Admin SDK,
    and returns the decoded token dict (contains 'email', 'uid', etc.).
    Raises 401 if the header is missing/malformed or the token is invalid.
    """
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
