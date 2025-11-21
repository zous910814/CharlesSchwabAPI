# Schwab FastAPI Skeleton

A small FastAPI service that wraps Charles Schwab API calls (balances + orders) with config isolation and schema validation.

## Project layout
```
app/
  main.py              # FastAPI app with routers registered
  core/config.py       # Settings loaded from env/.env via pydantic
  clients/schwab.py    # Schwab API client with token refresh + HTTP calls
  routers/accounts.py  # Accounts routes (balances)
  routers/orders.py    # Orders routes (place order)
  routers/auth.py      # OAuth callback to convert auth code -> tokens
  schemas/order.py     # Pydantic order payload models
main.py                # Re-exports app for `uvicorn main:app`
schwab_client.py       # Re-exports SchwabClient for convenience
requirements.txt
.env.example
```

## Setup
1) Python 3.10+ recommended. Install deps:
```bash
pip install -r requirements.txt
```
2) Copy `.env.example` to `.env` and fill in Schwab credentials and account details.
3) Start the server:
```bash
uvicorn app.main:app --reload
# or uvicorn main:app --reload
```

## API
- `GET /health` — basic health check with configured default account id
- `GET /accounts/{account_id}/balances` — fetch balances for a specific account
- `GET /accounts/default/balances` — fetch balances using the configured default account
- `POST /orders/{account_id}` — place an order for a specific account
- `POST /orders/default` — place an order for the configured default account
- `GET /accounts/callback?code=...&state=...` — OAuth redirect target; exchanges `code` for access/refresh tokens

`OrderRequest` payload (example):
```json
{
  "orderType": "MARKET",
  "session": "NORMAL",
  "duration": "DAY",
  "orderStrategyType": "SINGLE",
  "orderLegCollection": [
    {"instruction": "BUY", "quantity": 10, "symbol": "AAPL", "assetType": "EQUITY"}
  ]
}
```

## Notes
- `SCHWAB_BASE_URL` defaults to `https://api.schwabapi.com`; swap to the sandbox base if provided by Schwab.
- Initial OAuth authorization to obtain the first refresh token must be handled separately; store the refresh token securely.
- Add logging, retries, and stricter order models as you refine supported order types.
- If your Schwab app lists a callback like `https://127.0.0.1:8000/accounts/callback`, run the FastAPI server on that host/port and use the same URL in Schwab app settings. If you're not terminating TLS locally, set the callback to `http://127.0.0.1:8000/accounts/callback` to match the protocol you actually use.
