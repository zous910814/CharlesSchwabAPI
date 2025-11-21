from fastapi import FastAPI

from app.core.config import get_settings
from app.routers import accounts, orders, auth, market_data

settings = get_settings()

app = FastAPI(
    title="Schwab FastAPI",
    version="0.1.0",
    contact={"name": "API"},
)

app.include_router(accounts.router)
app.include_router(orders.router)
app.include_router(auth.router)
app.include_router(market_data.router)


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "account": settings.schwab_account_id}
