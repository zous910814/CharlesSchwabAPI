import asyncio
import re
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

    def _api_root(self) -> str:
        # base_url is configured with a trailing /v1 for the oauth/token calls;
        # trading and market-data endpoints hang off the bare host with their
        # own /trader/v1 or /marketdata/v1 prefix, so strip it here.
        return self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    async def _orders_request(self, path: str, account_id: str, order_body: dict) -> dict:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        await self._throttle()
        response = await self._get_http().post(
            f"{self._api_root()}/trader/v1/accounts/{account_id}/{path}",
            headers=headers,
            json=order_body,
        )
        response.raise_for_status()
        # Schwab's order endpoint returns 201 with no body; fall back to headers.
        if not response.content:
            return {"status_code": response.status_code, "location": response.headers.get("location")}
        return response.json()

    async def place_order(self, account_id: str, order_body: dict) -> dict:
        return await self._orders_request("orders", account_id, order_body)

    async def preview_order(self, account_id: str, order_body: dict) -> dict:
        """Validate an order (commission/cost estimate, margin/balance checks)
        without executing it — Schwab's previewOrder endpoint."""
        return await self._orders_request("previewOrder", account_id, order_body)

    async def get_account(self, account_id: str, fields: Optional[str] = "positions") -> dict:
        """Account details: securitiesAccount.currentBalances (buying power) and,
        with fields=positions, securitiesAccount.positions (current holdings)."""
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {"fields": fields} if fields else None
        await self._throttle()
        response = await self._get_http().get(
            f"{self._api_root()}/trader/v1/accounts/{account_id}",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def get_price_history(self, symbol: str, **query: object) -> dict:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {"symbol": symbol}
        params.update({k: v for k, v in query.items() if v is not None})
        await self._throttle()
        response = await self._get_http().get(
            f"{self._api_root()}/marketdata/v1/pricehistory",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def get_movers(self, symbol_id: str, *, sort: str = "VOLUME", frequency: int = 0) -> dict:
        """Today's top movers for an index (symbol_id e.g. '$DJI', '$COMPX', '$SPX').
        sort: VOLUME | TRADES | PERCENT_CHANGE_UP | PERCENT_CHANGE_DOWN.
        frequency: 0 (all day) | 1 | 5 | 10 | 30 | 60 (minutes)."""
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {"sort": sort, "frequency": frequency}
        await self._throttle()
        response = await self._get_http().get(
            f"{self._api_root()}/marketdata/v1/movers/{symbol_id}",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def get_quotes(self, symbols: list[str]) -> dict:
        """Real-time quotes (bid/ask/last) for one or more symbols, keyed by symbol."""
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {"symbols": ",".join(symbols)}
        await self._throttle()
        response = await self._get_http().get(
            f"{self._api_root()}/marketdata/v1/quotes",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def get_fundamentals(self, symbol: str) -> dict:
        """Fundamental data (P/E, EPS, margins, dividend yield, etc.) for a
        single symbol via Schwab's instruments endpoint. One symbol per call —
        unlike get_quotes, the fundamental projection doesn't take a
        comma-separated batch."""
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        # Schwab wants share-class tickers as "BRK/B", not the "BRK.B" dot
        # notation common in constituent lists (e.g. sp500_constituents.json)
        # — passing the dot form silently matches zero instruments. Only the
        # outbound API param is rewritten; callers keep using the dot form as
        # the row key, so it still joins against price_history / watchlist.
        api_symbol = re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1/\2", symbol)
        params = {"symbol": api_symbol, "projection": "fundamental"}
        await self._throttle()
        response = await self._get_http().get(
            f"{self._api_root()}/marketdata/v1/instruments",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        return response.json()
