"""One-time device-flow authorization that writes only an encrypted MSAL cache."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import msal

from ai_daily.mail import EncryptedTokenCache, MailAuthError

_AUTHORITY = "https://login.microsoftonline.com/consumers"
_SCOPE = ["Mail.Send"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an encrypted Outlook delegated token cache")
    parser.add_argument("--replace", action="store_true", help="replace an existing encrypted token cache")
    parser.add_argument("--cache-path", type=Path, help=argparse.SUPPRESS)
    return parser


def _cache_path(argument: Path | None) -> Path:
    if argument is not None:
        return argument
    return Path(__file__).resolve().parents[1] / "data" / "microsoft-token.enc"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = _cache_path(args.cache_path)
    if path.exists() and not args.replace:
        print("An encrypted Microsoft token cache already exists; use --replace to renew it.", file=sys.stderr)
        return 2
    client_id = os.environ.get("MS_CLIENT_ID")
    token_key = os.environ.get("MS_TOKEN_KEY")
    if not client_id or not token_key:
        print("Microsoft setup configuration is unavailable.", file=sys.stderr)
        return 2
    try:
        cache = EncryptedTokenCache.empty()
        application = msal.PublicClientApplication(
            client_id, authority=_AUTHORITY, token_cache=cache
        )
        flow = application.initiate_device_flow(scopes=_SCOPE)
        verification_uri = flow.get("verification_uri")
        user_code = flow.get("user_code")
        if not isinstance(verification_uri, str) or not isinstance(user_code, str):
            raise MailAuthError("Microsoft device authorization is unavailable")
        print(f"Open {verification_uri} and enter code {user_code}.")
        result = application.acquire_token_by_device_flow(flow)
        if not isinstance(result, dict) or not result.get("access_token"):
            raise MailAuthError("Microsoft device authorization did not complete")
        EncryptedTokenCache.save(cache, path, token_key, replace=args.replace)
    except Exception:  # noqa: BLE001 - provider details can contain credentials or tokens
        print("Microsoft authorization setup failed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
