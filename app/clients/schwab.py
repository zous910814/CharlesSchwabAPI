import asyncio
import time
from collections import deque
from typing import Optional

import httpx

# Schwab's documented app-wide limit is 120 requests/minute; stay under it to
# avoid 429s (each one costs a multi-second retry backoff and slows bulk jobs
# far more than a slightly conservative rate limit does).
DEFAULT_RATE_LIMIT_PER_MINUTE = 110


class SchwabClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        refresh_token: str,
        base_url: str,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.refresh_token = refresh_token
        self.base_url = base_url.rstrip("/")

        self.access_token: Optional[str] = None
        self.expires_at: float = 0

        self._http: Optional[httpx.AsyncClient] = None
        self._rate_limit_per_minute = rate_limit_per_minute
        self._request_times: deque[float] = deque()
        self._rate_lock = asyncio.Lock()

    async def __aenter__(self) -> "SchwabClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def _get_http(self) -> httpx.AsyncClient:
        # Reused across calls so requests share one pooled, keep-alive
        # connection instead of paying a fresh TLS handshake every time.
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._http

    async def _throttle(self) -> None:
        window = 60.0
        async with self._rate_lock:
            while True:
                now = time.monotonic()
                while self._request_times and now - self._request_times[0] > window:
                    self._request_times.popleft()
                if len(self._request_times) < self._rate_limit_per_minute:
                    self._request_times.append(now)
                    return
                await asyncio.sleep(window - (now - self._request_times[0]) + 0.05)

    async def _refresh_token(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        await self._throttle()
        response = await self._get_http().post(
            f"{self.base_url}/oauth/token",
            data=data,
            auth=(self.client_id, self.client_secret),
        )
        response.raise_for_status()
        token_payload = response.json()

        self.access_token = token_payload["access_token"]
        self.expires_at = time.time() + token_payload.get("expires_in", 0) - 60

    async def _ensure_token(self) -> str:
        if not self.access_token or time.time() >= self.expires_at:
            await self._refresh_token()
        return self.access_token  # type: ignore[return-value]

    async def exchange_code_for_tokens(self, code: str) -> dict:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        await self._throttle()
        response = await self._get_http().post(
            f"{self.base_url}/oauth/token",
            data=data,
            auth=(self.client_id, self.client_secret),
        )
        response.raise_for_status()
        tokens = response.json()
        # Caller is responsible for persisting refresh_token securely
        return tokens
