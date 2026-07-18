"""OAuth 2.0 for the two user tokens the system needs (Desktop / installed-app).

There are deliberately **two separate tokens**, never one combined token:

  * ``drive``  — `drive.file` only. The ingest service uploads meal photos into the
    user's own Drive with it (a service account has no Drive storage quota).
  * ``health`` — the Google Health read scopes only. The daily job pulls Fitbit
    biometrics with it.

They CANNOT be merged. The Google Health API rejects any access token that also
carries a Drive scope (`403 PERMISSION_DENIED / DISALLOWED_OAUTH_SCOPES`), which
is what broke the daily job on 2026-07-10. Keep them apart, in separate secrets.

Run once per token, then push each to Secret Manager:

    python -m src.authenticate drive
    gcloud secrets versions add drive-oauth-token  --data-file=credentials/token_drive.json

    python -m src.authenticate health
    gcloud secrets versions add health-oauth-token --data-file=credentials/token_health.json

The OAuth consent app must be **In production**, or refresh tokens expire after 7
days. Only needed again if a token is revoked or a scope is added.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_DIR = BASE_DIR / "credentials"
CLIENT_SECRETS_FILE = CREDENTIALS_DIR / "oauth_client.json"

_GH = "https://www.googleapis.com/auth/googlehealth."

# Health scopes, one per data family we read (see src/google_health.py DATA_TYPES):
#   sleep                        -> sleep stages/summary
#   health_metrics_and_measurements -> resting HR, HRV, SpO2, skin temp, respiration
#   activity_and_fitness         -> steps, distance, floors, active minutes/zone
#                                   minutes, energy burned, VO2 max
# All read-only: this system never writes back to Google Health.
HEALTH_SCOPES: List[str] = [
    f"{_GH}sleep.readonly",
    f"{_GH}health_metrics_and_measurements.readonly",
    f"{_GH}activity_and_fitness.readonly",
]

# drive.file only: lets us upload meal photos into the user's own Drive storage and
# touch nothing else there.
DRIVE_SCOPES: List[str] = ["https://www.googleapis.com/auth/drive.file"]

PROFILES: Dict[str, List[str]] = {"drive": DRIVE_SCOPES, "health": HEALTH_SCOPES}


def token_file(profile: str) -> Path:
    return CREDENTIALS_DIR / f"token_{profile}.json"


def get_credentials(profile: str = "drive", interactive: bool = True) -> Credentials:
    """Return valid credentials for `profile` ("drive" or "health"), refreshing or
    prompting as needed. Each profile has its own token file and its own scopes —
    mixing them is what triggers the Health API's 403."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {list(PROFILES)}")
    scopes = PROFILES[profile]
    path = token_file(profile)

    creds: Optional[Credentials] = None
    if path.exists():
        creds = Credentials.from_authorized_user_file(str(path), scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, path)
            return creds
        except Exception:
            creds = None  # refresh failed — fall through to interactive login

    if not interactive:
        raise RuntimeError(
            f"No valid {profile} credentials. Run `python -m src.authenticate {profile}`."
        )

    if not CLIENT_SECRETS_FILE.exists():
        raise FileNotFoundError(
            f"Missing OAuth client file: {CLIENT_SECRETS_FILE}\n"
            "Create it from your Google Cloud OAuth (Desktop) client."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), scopes)
    creds = flow.run_local_server(port=0, prompt="consent")
    _save_token(creds, path)
    return creds


def _save_token(creds: Credentials, path: Path) -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    os.chmod(path, 0o600)
