#!/usr/bin/env python
"""Convenience launcher: starts the API and opens the Schwab login page in your browser.

Host/port/TLS are derived from SCHWAB_REDIRECT_URI in .env, so this stays in
sync with whatever callback URL you've registered with Schwab.
"""
from __future__ import annotations

import threading
import webbrowser
from urllib.parse import urlparse

import uvicorn

from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    redirect = urlparse(settings.schwab_redirect_uri)
    host = redirect.hostname or "127.0.0.1"
    port = redirect.port or (443 if redirect.scheme == "https" else 8000)
    login_url = f"{redirect.scheme}://{redirect.netloc}/accounts/login"

    # Runs once in the reload-supervisor process, not on every --reload restart.
    # Delay gives uvicorn time to bind the port before the browser requests it.
    threading.Timer(1.5, webbrowser.open, args=(login_url,)).start()

    uvicorn_kwargs: dict = {"host": host, "port": port, "reload": True}
    if redirect.scheme == "https":
        uvicorn_kwargs["ssl_certfile"] = "cert.pem"
        uvicorn_kwargs["ssl_keyfile"] = "key.pem"

    uvicorn.run("app.main:app", **uvicorn_kwargs)


if __name__ == "__main__":
    main()
