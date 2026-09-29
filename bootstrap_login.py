"""One-time interactive Weverse login bootstrap.

Weverse retired the legacy password-only token endpoint (requests now fail
with code -10004). The current contract is:

  1. POST /web/api/v4/auth/token/by-credentials  {email, password, otpSessionId}
     -> responds -25044 "Email OTP verification is required." and emails a code
  2. POST /web/api/v3/auth/token/by-credentials-with-otp
        {email, password, otpSessionId, otpCode}
     -> returns accessToken / refreshToken

This script performs that flow once and appends the tokens to the local
git-ignored .env. Later runs of the app refresh the access token from
WEVERSE_REFRESH_TOKEN without another OTP.

Usage:
  python bootstrap_login.py                # fully interactive
  python bootstrap_login.py --request-otp  # phase 1 only (automation-friendly)
  python bootstrap_login.py --verify-code 123456   # phase 2 only

Prints only masked confirmations and safe error codes. Never prints
credentials or tokens. Requires WEVERSE_USERNAME and WEVERSE_PASSWORD in .env.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

import os

ACCOUNT_API = "https://accountapi.weverse.io"
BY_CREDENTIALS_URL = f"{ACCOUNT_API}/web/api/v4/auth/token/by-credentials"
WITH_OTP_URL = f"{ACCOUNT_API}/web/api/v3/auth/token/by-credentials-with-otp"
OTP_SESSION_STATE = Path(".weverse_otp_session.json")

APP_SECRET = "5419526f1c624b38b10787e5c10b2a7a"


def _headers() -> dict:
    device_id = os.environ.get("WEVERSE_DEVICE_ID") or str(uuid.uuid4())
    return {
        "Content-Type": "application/json",
        "X-ACC-TRACE-ID": str(uuid.uuid4()),
        "X-ACC-APP-VERSION": "4.8.0",
        "X-ACC-APP-SECRET": APP_SECRET,
        "X-ACC-SERVICE-ID": "wemember",
        "X-ACC-LANGUAGE": "en",
        "X-CLOG-USER-DEVICE-ID": device_id,
        "X-ACC-DEVICE-ID": str(uuid.uuid4()),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }


def _error_fields(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {"non_json_body_bytes": len(resp.text)}
    if isinstance(data, dict):
        return {
            k: str(v)[:120]
            for k, v in data.items()
            if any(t in k.lower() for t in ("error", "code", "message", "status", "reason"))
        }
    return {"unexpected_shape": type(data).__name__}


def request_otp() -> int:
    username = os.environ.get("WEVERSE_USERNAME", "").strip()
    password = os.environ.get("WEVERSE_PASSWORD", "")
    if not username or not password:
        print("missing_credentials: set WEVERSE_USERNAME and WEVERSE_PASSWORD in .env first.")
        return 1

    session_id = uuid.uuid4().hex
    resp = requests.post(
        BY_CREDENTIALS_URL,
        json={"email": username, "password": password, "otpSessionId": session_id},
        headers=_headers(),
        timeout=30,
    )
    fields = _error_fields(resp)
    if resp.status_code == 200:
        print("No OTP challenge was returned (status 200). If this account skips OTP,")
        print("response keys are:", fields or "success")
        OTP_SESSION_STATE.write_text(json.dumps({"otpSessionId": session_id}), encoding="utf-8")
        return 0
    # Expected: -25044 Email OTP verification is required (the code was emailed).
    print("phase-1 status:", resp.status_code, "fields:", fields)
    if str(fields.get("code", "")).startswith("-25"):
        OTP_SESSION_STATE.write_text(json.dumps({"otpSessionId": session_id}), encoding="utf-8")
        print(f"OTP code was emailed to {username[0]}***{username[username.find('@'):]}.")
        print("Run: python bootstrap_login.py --verify-code <code>")
        return 0
    print("Unexpected response; not saving session state.")
    return 1


def verify_code(code: str) -> int:
    username = os.environ.get("WEVERSE_USERNAME", "").strip()
    password = os.environ.get("WEVERSE_PASSWORD", "")
    if not username or not password:
        print("missing_credentials: set WEVERSE_USERNAME and WEVERSE_PASSWORD in .env first.")
        return 1
    if not OTP_SESSION_STATE.exists():
        print("no OTP session found. Run: python bootstrap_login.py --request-otp")
        return 1
    session_id = json.loads(OTP_SESSION_STATE.read_text(encoding="utf-8"))["otpSessionId"]

    body = {
        "email": username,
        "password": password,
        "otpSessionId": session_id,
        "otpCode": code.strip(),
    }
    resp = requests.post(WITH_OTP_URL, json=body, headers=_headers(), timeout=30)
    fields = _error_fields(resp)
    if resp.status_code != 200:
        print("phase-2 failed:", resp.status_code, "fields:", fields)
        return 1
    try:
        data = resp.json()
    except ValueError:
        print("phase-2 returned non-JSON success response.")
        return 1

    access = data.get("accessToken") or data.get("access_token")
    refresh = data.get("refreshToken") or data.get("refresh_token")
    if not access or not refresh:
        print("phase-2 succeeded but response lacked token fields. Keys:", sorted(data.keys()))
        return 1

    env_path = Path(".env")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    keep = [l for l in lines if not l.startswith(("WEVERSE_ACCESS_TOKEN=", "WEVERSE_REFRESH_TOKEN="))]
    keep += [f"WEVERSE_ACCESS_TOKEN={access}", f"WEVERSE_REFRESH_TOKEN={refresh}"]
    env_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    OTP_SESSION_STATE.unlink(missing_ok=True)
    print(f"tokens saved to .env (access {len(access)} chars, refresh {len(refresh)} chars).")
    print("Never commit .env. Re-run this script when the refresh token expires.")
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-otp", action="store_true", help="phase 1: trigger the OTP email")
    parser.add_argument("--verify-code", metavar="CODE", help="phase 2: complete login with the emailed code")
    args = parser.parse_args()

    if args.request_otp:
        return request_otp()
    if args.verify_code:
        return verify_code(args.verify_code)

    # Fully interactive mode.
    if request_otp() != 0:
        return 1
    code = input("Enter the OTP code from your email: ")
    return verify_code(code)


if __name__ == "__main__":
    sys.exit(main())
