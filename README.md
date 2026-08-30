# Schwab FastAPI Skeleton

A minimal FastAPI service that handles Charles Schwab OAuth: log in, get an auth code, exchange it for tokens.

## Project layout
```
app/
  main.py              # FastAPI app with routers registered
  core/config.py       # Settings loaded from env/.env via pydantic
  clients/schwab.py     # Schwab API client: token refresh + code exchange
  routers/accounts.py   # GET /accounts/login — starts the OAuth flow
  routers/auth.py       # GET /accounts/callback — exchanges code for tokens
requirements.txt
.env
cert.pem / key.pem      # local TLS certs for the HTTPS redirect URI
```

## Setup
1) Python 3.10+ recommended. Install deps:
```bash
pip install -r requirements.txt
```
2) Fill in Schwab credentials and account details in `.env`.
3) Start the server:
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --ssl-certfile cert.pem --ssl-keyfile key.pem --reload
```

## API
- `GET /health` — basic health check with configured default account id
- `GET /accounts/login` — returns/renders the Schwab authorize URL to start login
- `GET /accounts/callback?code=...&state=...` — OAuth redirect target; exchanges `code` for access/refresh tokens, persists them to `.schwab_tokens.json`, and updates `SCHWAB_REFRESH_TOKEN` in `.env`

## Notes
- `SCHWAB_BASE_URL` defaults to `https://api.schwabapi.com`; swap to the sandbox base if provided by Schwab.
- If your Schwab app lists a callback like `https://127.0.0.1:8000/accounts/callback`, run the FastAPI server on that host/port and use the same URL in Schwab app settings. If you're not terminating TLS locally, set the callback to `http://127.0.0.1:8000/accounts/callback` to match the protocol you actually use.
