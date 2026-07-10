"""OAuth 2.0 handling for the Google Health API (Desktop / installed-app flow).

First run opens a browser for consent and stores a refreshable token in
credentials/token.json. Subsequent runs are non-interactive: the stored
refresh token is used automatically.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_DIR = BASE_DIR / "credentials"
CLIENT_SECRETS_FILE = CREDENTIALS_DIR / "oauth_client.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.json"

# Read-only access to body metrics & measurements (weight, body-fat, ...), plus
# drive.file so meal photos can be uploaded into the user's own Drive storage.
# (A service account has no Drive quota, so photo uploads must run as the user.)
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


def get_credentials(interactive: bool = True) -> Credentials:
    """Return valid Google credentials, refreshing or prompting as needed."""
    creds: Optional[Credentials] = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception:
            creds = None  # refresh failed — fall through to interactive login

    if not interactive:
        raise RuntimeError(
            "No valid credentials found. Run `python -m src.authenticate` once to log in."
        )

    if not CLIENT_SECRETS_FILE.exists():
        raise FileNotFoundError(
            f"Missing OAuth client file: {CLIENT_SECRETS_FILE}\n"
            "Create it from your Google Cloud OAuth (Desktop) client."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    _save_token(creds)
    return creds


def _save_token(creds: Credentials) -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)
