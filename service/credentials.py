"""Credential loading helpers.

Credentials are stored as standalone JSON files on disk (gitignored).
Environment variables hold the file path, not the credential value itself.

Usage:
    creds = load_json_credential("VENDOR_AWS_BILLING_CREDENTIALS")
    # → reads the path from the env var, opens the file, returns parsed JSON
"""

import json
import os


def load_json_credential(env_var: str) -> dict:
    """Load a JSON credential from the file path stored in env_var."""
    path = os.environ[env_var]
    with open(path) as f:
        return json.load(f)


def load_text_credential(env_var: str) -> str:
    """Load a plain-text credential from the file path stored in env_var."""
    path = os.environ[env_var]
    with open(path) as f:
        return f.read().strip()
