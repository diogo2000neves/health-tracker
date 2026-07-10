"""One-time login. Run once to authorise and store a refreshable token.

    python -m src.authenticate
"""
from __future__ import annotations

from src.auth import TOKEN_FILE, get_credentials


def main() -> None:
    get_credentials(interactive=True)
    print(f"Authentication complete. Token saved to {TOKEN_FILE}")


if __name__ == "__main__":
    main()
