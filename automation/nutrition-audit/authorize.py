#!/usr/bin/env python3
"""One-time OAuth consent for the local Claude nutrition-audit job.

Mints a single user token carrying BOTH the Sheets and Drive scopes the audit
needs, saved next to the backend's other credentials. Deliberately separate from
the health/drive tokens in src/auth.py: those are scoped narrowly for the cloud
job and must never carry a Sheets scope, and the Health API even 403s on any token
that also carries Drive. This token never touches the Health API, so combining
Sheets + Drive on it is fine.

  * spreadsheets  — read the `meals` tab and write revised rows back. The user owns
    the Sheet, so their own account has Editor rights.
  * drive.file    — download the meal photos to show Claude. The photos were created
    by this SAME OAuth client (the ingest service's drive token), so `drive.file`
    (per-file access to files the app created) can read them. If that turns out not
    to hold, re-run with `--drive-readonly` for a broader (read-only) Drive scope.

Run once:

    backend/venv/bin/python automation/nutrition-audit/authorize.py

A browser opens for consent; the token is written to
backend/credentials/token_nutrition_audit.json (0600, git-ignored). Only needed
again if the token is revoked or a scope changes.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# automation/nutrition-audit/authorize.py -> repo root -> backend/credentials
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CREDENTIALS_DIR = REPO_ROOT / "backend" / "credentials"
CLIENT_SECRETS_FILE = CREDENTIALS_DIR / "oauth_client.json"
TOKEN_FILE = CREDENTIALS_DIR / "token_nutrition_audit.json"

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drive-readonly", action="store_true",
        help="Use the broader drive.readonly scope instead of drive.file "
             "(only if drive.file cannot read the meal photos).")
    args = parser.parse_args()

    if not CLIENT_SECRETS_FILE.exists():
        print(f"ERROR: missing OAuth client file: {CLIENT_SECRETS_FILE}",
              file=sys.stderr)
        return 1

    drive_scope = DRIVE_READONLY_SCOPE if args.drive_readonly else DRIVE_FILE_SCOPE
    scopes = [SHEETS_SCOPE, drive_scope]
    print(f"Requesting consent for scopes:\n  - {SHEETS_SCOPE}\n  - {drive_scope}\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), scopes)
    creds = flow.run_local_server(port=0, prompt="consent")

    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)
    print(f"\nToken saved to {TOKEN_FILE}")
    print("Next: backend/venv/bin/python automation/nutrition-audit/audit.py --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
