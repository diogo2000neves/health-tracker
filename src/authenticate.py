"""One-time login. Authorises Drive access and stores a refreshable token.

    python -m src.authenticate

Then push it to Secret Manager, which is where the ingest service reads it from:

    gcloud secrets versions add drive-oauth-token --data-file=credentials/token.json

Only needed if the token is ever revoked — the refresh token does not expire while
the OAuth app is "In production".
"""
from __future__ import annotations

from src.auth import TOKEN_FILE, get_credentials


def main() -> None:
    get_credentials(interactive=True)
    print(f"Authentication complete. Token saved to {TOKEN_FILE}")


if __name__ == "__main__":
    main()
