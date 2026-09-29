"""Signed Weverse gateway client with bearer-token auth and auto-refresh.

Weverse retired both the pinned library's login endpoint (replies -10004 to
everything) and its content host (DNS removed). Reads go through the Naver
gateway with an HMAC-SHA1 request signature; auth is a bearer token obtained
once by a human (browser login or bootstrap_login.py) and stored in .env.

Token lifecycle:
  - access token: ~3 days, passed as WEVERSE_ACCESS_TOKEN
  - refresh token: ~90 days, passed as WEVERSE_REFRESH_TOKEN, and it ROTATES
  - on a 401 this client exchanges the refresh token at
    POST accountapi.weverse.io/api/v1/token/refresh, keeps the new pair in
    memory, and best-effort persists the rotated pair back to .env
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

API_BASE = "https://global.apis.naver.com/weverse/wevweb"
ACCOUNT_API = "https://accountapi.weverse.io"
REFRESH_URL = f"{ACCOUNT_API}/api/v1/token/refresh"
HMAC_ACTIVE_KEY = "1b9cb6378d959b45714bec49971ade22e6e24e42"
APP_ID = "be4d79eb8fc7bd008ee82c8ec4ff6fd4"

COMMON_PARAMS = {"appId": APP_ID, "language": "en", "os": "WEB", "platform": "WEB", "wpf": "pc"}
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class GatewayError(Exception):
    """Adapter-internal error carrying a stable tool-layer error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MissingCredentials(GatewayError):
    def __init__(self):
        super().__init__(
            "missing_credentials",
            "Weverse is not configured. Set WEVERSE_ACCESS_TOKEN and WEVERSE_REFRESH_TOKEN "
            "in the local .env (see docs/TOOLS.md).",
        )


class AuthenticationFailed(GatewayError):
    def __init__(self):
        super().__init__(
            "authentication_failed",
            "Weverse rejected the configured tokens. Re-authenticate: log into weverse.io "
            "in a browser and refresh the token values in .env (see docs/TOOLS.md).",
        )


class RateLimited(GatewayError):
    def __init__(self):
        super().__init__("rate_limited", "Weverse is rate limiting requests. Try again later.")


class NotFound(GatewayError):
    """HTTP 404: the resource exists no more, so retrying cannot help."""

    def __init__(self):
        super().__init__("community_not_joined", "Weverse has no data at that address.")


class UpstreamSchemaChanged(GatewayError):
    def __init__(self, detail: str):
        super().__init__("upstream_schema_changed", f"Weverse response format changed: {detail}")


class Timeout(GatewayError):
    def __init__(self):
        super().__init__("timeout", "Weverse request exceeded the time budget.")


class UnexpectedUpstream(GatewayError):
    def __init__(self):
        super().__init__("unexpected_upstream_error", "Weverse request failed unexpectedly. Try again later.")


def sign_request(path: str, params: dict, timestamp_ms: int | None = None) -> dict:
    """Return (url, params) with wmd/wmsgpad attached to a signed request.

    Signed payload: (short_path + "?" + sorted_query)[:255] + wmsgpad,
    HMAC-SHA1 with the active key, Base64. The short path excludes the
    /weverse/wevweb gateway prefix. The wire order must equal the signed
    (sorted) order because the gateway recomputes the HMAC over the received
    query string; wmd travels as a normal param so the HTTP layer URL-encodes
    the Base64 (+ and / would otherwise corrupt the signature).
    """
    query = urlencode(sorted(params.items()))
    wmsgpad = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    payload = f"{path}?{query}"[:255] + wmsgpad
    digest = hmac.new(HMAC_ACTIVE_KEY.encode(), payload.encode(), hashlib.sha1).digest()
    signed = dict(sorted(params.items()))
    signed["wmd"] = base64.b64encode(digest).decode()
    signed["wmsgpad"] = wmsgpad
    return {"url": f"{API_BASE}{path}", "params": signed}


class WeverseGatewayClient:
    def __init__(
        self,
        getter=None,
        poster=None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        env_path: str | os.PathLike | None = None,
    ):
        load_dotenv()
        self._lock = threading.Lock()
        self._getter = getter or requests.get
        self._poster = poster or requests.post
        self._access = access_token
        self._refresh = refresh_token
        self._env_path = Path(env_path) if env_path else None

    # -- token state -----------------------------------------------------------

    @staticmethod
    def _env(key: str) -> str:
        return (os.environ.get(key) or "").strip()

    def _tokens(self) -> tuple[str, str]:
        access = (self._access or self._env("WEVERSE_ACCESS_TOKEN")).strip()
        refresh = (self._refresh or self._env("WEVERSE_REFRESH_TOKEN")).strip()
        return access, refresh

    def refresh_tokens(self) -> None:
        """Exchange the refresh token for a fresh pair (called on 401 or cold start)."""
        with self._lock:
            _, refresh = self._tokens()
            if not refresh:
                raise MissingCredentials()
            try:
                resp = self._poster(
                    REFRESH_URL,
                    json={"refreshToken": refresh},
                    headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA},
                    timeout=30,
                )
            except requests.RequestException:
                raise UnexpectedUpstream()
            if resp.status_code in (401, 403):
                raise AuthenticationFailed()
            if resp.status_code == 429:
                raise RateLimited()
            if resp.status_code != 200:
                raise UnexpectedUpstream()
            try:
                data = resp.json()
            except ValueError:
                raise UpstreamSchemaChanged("non-JSON refresh response")
            access = data.get("accessToken")
            new_refresh = data.get("refreshToken")
            if not isinstance(access, str) or not access:
                raise UpstreamSchemaChanged("refresh response without accessToken")
            self._access = access
            if isinstance(new_refresh, str) and new_refresh:
                # The refresh token rotates on use; remember the newest one.
                self._refresh = new_refresh
                self._persist_refresh(new_refresh)

    def _persist_refresh(self, refresh_token: str) -> None:
        """Best-effort: replace WEVERSE_REFRESH_TOKEN in the local .env.

        In-memory tokens keep working even if this fails (e.g. read-only FS on
        Cloud Run); persistence only matters across process restarts.
        """
        env_path = self._env_path or Path(".env")
        try:
            if not env_path.exists():
                return
            lines = env_path.read_text(encoding="utf-8").splitlines()
            replaced = False
            for index, line in enumerate(lines):
                if line.startswith("WEVERSE_REFRESH_TOKEN="):
                    lines[index] = f"WEVERSE_REFRESH_TOKEN={refresh_token}"
                    replaced = True
            if not replaced:
                lines.append(f"WEVERSE_REFRESH_TOKEN={refresh_token}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:  # noqa: S110
            pass

    # -- signed reads ------------------------------------------------------------

    def _headers(self, access: str) -> dict:
        return {
            "Authorization": f"Bearer {access}",
            "Referer": "https://weverse.io/",
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
        }

    def _signed_get(self, path: str, params: dict, deadline: float) -> requests.Response:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Timeout()
        access, _ = self._tokens()
        request = sign_request(path, {**COMMON_PARAMS, **params})
        try:
            return self._getter(
                request["url"],
                params=request["params"],
                headers=self._headers(access),
                timeout=max(1, int(remaining)),
            )
        except requests.RequestException:
            raise UnexpectedUpstream()

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> dict:
        if resp.status_code == 401:
            raise AuthenticationFailed()
        if resp.status_code == 404:
            raise NotFound()
        if resp.status_code == 429:
            raise RateLimited()
        if resp.status_code != 200:
            raise UnexpectedUpstream()
        try:
            data = resp.json()
        except ValueError:
            raise UpstreamSchemaChanged("non-JSON body")
        if not isinstance(data, dict):
            raise UpstreamSchemaChanged("non-object body")
        return data

    def get_json(self, path: str, extra_params: dict | None = None, deadline: float | None = None) -> dict:
        """Signed GET with one automatic refresh-and-retry on 401."""
        deadline = deadline if deadline is not None else time.monotonic() + 120
        access, refresh = self._tokens()
        if not access and not refresh:
            raise MissingCredentials()
        if not access:
            # Cold start with only a refresh token configured.
            self.refresh_tokens()

        resp = self._signed_get(path, extra_params or {}, deadline)
        if resp.status_code == 401:
            self.refresh_tokens()
            resp = self._signed_get(path, extra_params or {}, deadline)
        return self._raise_for_status(resp)
