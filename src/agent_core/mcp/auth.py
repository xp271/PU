"""
OAuth 2.0 / 2.1 authentication for MCP servers.

Supports client credentials, authorization code with PKCE, device code
flow, and token refresh.  The :class:`OAuthTokenManager` is lightweight
and deliberately decoupled from the transport layer — it resolves a
bearer token that the transport injects into HTTP headers.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Any

import httpx

from ..models import McpOAuthConfig

logger = logging.getLogger("agent_core.mcp.auth")


class OAuthTokenManager:
    """Manages the OAuth token lifecycle for one MCP server."""

    def __init__(self, config: McpOAuthConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_valid_token(self) -> str | None:
        """Return a valid access token, refreshing if needed.

        Returns ``None`` when no token is available and automatic
        acquisition (e.g. client credentials) is not possible.
        """
        async with self._lock:
            if self._config.token:
                if self._needs_refresh():
                    await self._refresh_token()
                return self._config.token

            # Attempt automatic acquisition via client credentials
            if self._config.client_secret:
                await self._client_credentials_flow()
                return self._config.token

            return None

    async def start_authorization_code_flow(
        self, code: str, code_verifier: str | None = None
    ) -> str:
        """Exchange an authorization code for tokens."""
        async with self._lock:
            payload: dict[str, Any] = {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._config.client_id,
                "redirect_uri": self._config.redirect_uri,
            }
            if self._config.client_secret:
                payload["client_secret"] = self._config.client_secret
            if code_verifier:
                payload["code_verifier"] = code_verifier
            return await self._token_request(payload)

    async def start_device_code_flow(self) -> dict[str, str]:
        """Initiate device code flow.

        Returns ``{'device_code', 'user_code', 'verification_uri', ...}``.
        If the authorization server returns an access token immediately
        (device code specification allows this), it is stored.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._config.device_code_url,
                data={
                    "client_id": self._config.client_id,
                    "scope": " ".join(self._config.scopes),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._config.token = data.get("access_token")
            self._config.refresh_token = data.get("refresh_token")
            expires = data.get("expires_in")
            if expires and isinstance(expires, (int, float)):
                self._config.expires_at = _time.time() + expires
            return data

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _refresh_token(self) -> None:
        if not self._config.refresh_token:
            return
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._config.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._config.refresh_token,
                        "client_id": self._config.client_id,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._config.token = data["access_token"]
                self._config.refresh_token = data.get(
                    "refresh_token", self._config.refresh_token
                )
                expires = data.get("expires_in")
                if expires and isinstance(expires, (int, float)):
                    self._config.expires_at = _time.time() + expires
                logger.info("OAuth token refreshed for '%s'", self._config.client_id)
        except Exception as exc:
            logger.error("Token refresh failed: %s", exc)
            self._config.token = None

    async def _client_credentials_flow(self) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._config.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "scope": " ".join(self._config.scopes),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._config.token = data["access_token"]
            expires = data.get("expires_in")
            if expires and isinstance(expires, (int, float)):
                self._config.expires_at = _time.time() + expires

    async def _token_request(self, payload: dict[str, Any]) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(self._config.token_url, data=payload)
            resp.raise_for_status()
            data = resp.json()
            self._config.token = data["access_token"]
            self._config.refresh_token = data.get("refresh_token")
            expires = data.get("expires_in")
            if expires and isinstance(expires, (int, float)):
                self._config.expires_at = _time.time() + expires
            logger.info("OAuth token obtained for '%s'", self._config.client_id)
            return self._config.token

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _needs_refresh(self) -> bool:
        """Refresh once the token enters the last 20% of its lifetime
        (and always with at least a 60s safety margin)."""
        if self._config.expires_at is None:
            return False
        now = _time.time()
        ttl_remaining = self._config.expires_at - now
        margin = max(60.0, 0.2 * max(ttl_remaining, 0.0))
        return ttl_remaining < margin
