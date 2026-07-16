"""One-time login for a user token. Run once per profile.

    python -m src.authenticate health     # Google Health read scopes (daily job)
    python -m src.authenticate drive      # drive.file only (ingest photo upload)

Then push the token to Secret Manager, which is where the services read it from:

    gcloud secrets versions add health-oauth-token --data-file=credentials/token_health.json
    gcloud secrets versions add drive-oauth-token  --data-file=credentials/token_drive.json

The two MUST stay separate — the Health API 403s on a token that also carries a
Drive scope. Only needed again if a token is revoked or its scopes change.
"""
from __future__ import annotations

import sys

from src.auth import PROFILES, get_credentials, token_file


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "drive"
    if profile not in PROFILES:
        sys.exit(f"usage: python -m src.authenticate [{'|'.join(PROFILES)}]")
    get_credentials(profile, interactive=True)
    print(f"Authenticated '{profile}'. Token saved to {token_file(profile)}")
    print("Scopes granted:")
    for scope in PROFILES[profile]:
        print(f"  - {scope}")
    secret = f"{profile}-oauth-token"
    print(f"\nNow push it:\n  gcloud secrets versions add {secret} "
          f"--data-file={token_file(profile)}")


if __name__ == "__main__":
    main()
