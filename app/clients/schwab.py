import time
from typing import Optional

import httpx


class SchwabClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        refresh_token: str,
        code_verifier: Optional[str] = None,
        base_url: str,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.refresh_token = refresh_token
        self.code_verifier = code_verifier
        self.base_url = base_url.rstrip("/")

        self.access_token: Optional[str] = None
        self.expires_at: float = 0

    async def _refresh_token(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
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
        if self.code_verifier:
            data["code_verifier"] = self.code_verifier
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/oauth/token",
                data=data,
                auth=(self.client_id, self.client_secret),
            )
            response.raise_for_status()
            tokens = response.json()
        # Caller is responsible for persisting refresh_token securely
        return tokens

    async def get_account_balances(self, account_id: str) -> dict:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.base_url}/accounts/{account_id}/balances", headers=headers)
            response.raise_for_status()
            return response.json()

    async def place_order(self, account_id: str, order_body: dict) -> dict:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/accounts/{account_id}/orders",
                headers=headers,
                json=order_body,
            )
            response.raise_for_status()
            return response.json()

    async def get_price_history(self, symbol: str, **query: object) -> dict:
        """Fetch historical OHLCV candles for a symbol."""
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {"symbol": symbol}
        params.update({k: v for k, v in query.items() if v is not None})
        # Avoid double-versioning if base_url already ends with /v1
        api_root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{api_root}/marketdata/v1/pricehistory",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            return response.json()
