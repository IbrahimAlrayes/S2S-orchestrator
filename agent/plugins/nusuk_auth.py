from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import httpx

logger = logging.getLogger("nusuk-agent.auth")

_DEFAULT_TOKEN_TTL = 3600  # seconds; used as fallback when the JWT has no exp claim


class NusukAuthError(RuntimeError):
    pass


class NusukTokenManager:
    """Fetches and caches a Nusuk JWT using client_id/client_secret credentials."""

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        client: httpx.AsyncClient,
        *,
        user_id: str | None = None,
        refresh_margin_seconds: int = 60,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_id = user_id or client_id
        self._client = client
        self._refresh_margin = refresh_margin_seconds
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        now = time.time()
        if self._token and now + self._refresh_margin < self._expires_at:
            return self._token

        async with self._lock:
            now = time.time()
            if self._token and now + self._refresh_margin < self._expires_at:
                return self._token
            await self._refresh()
            if self._token is None:
                raise NusukAuthError("refresh produced no token")
            return self._token

    async def invalidate(self) -> None:
        async with self._lock:
            self._token = None
            self._expires_at = 0.0

    def seed_cache(self, token: str, expires_at: float) -> None:
        # Seed the JWT cache from an out-of-band fetch. Used by sync prewarm to
        # skip the auth RTT on the first job without sharing httpx connections
        # across event loops (which corrupts the connection pool).
        self._token = token
        self._expires_at = expires_at

    async def _refresh(self) -> None:
        url = f"{self._base_url}/auth/token"
        logger.info("nusuk_auth_refresh url=%s client_id=%s", url, self._client_id)
        try:
            response = await self._client.post(
                url,
                json={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "user_id": self._user_id,
                },
            )
        except httpx.HTTPError as exc:
            raise NusukAuthError(f"Nusuk auth request failed: {exc}") from exc

        if response.status_code >= 400:
            raise NusukAuthError(
                f"Nusuk auth rejected: status={response.status_code} body={response.text[:200]}"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise NusukAuthError(f"Nusuk auth response missing access_token: {payload}")

        self._token = token
        self._expires_at = _jwt_expiry(token) or (time.time() + _DEFAULT_TOKEN_TTL)
        logger.info(
            "nusuk_auth_ok expires_in_seconds=%d", max(0, int(self._expires_at - time.time()))
        )


def _jwt_expiry(token: str) -> float | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        logger.warning("nusuk_auth_jwt_decode_failed", exc_info=True)
    return None
