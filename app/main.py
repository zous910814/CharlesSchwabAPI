import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.core import activity_log, data_pipeline, fundamentals_db, monitor, price_history_db
from app.core.config import get_settings
from app.routers import accounts, auth, dashboard, line_webhook, orders

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await activity_log.init_db()
    await price_history_db.init_pool(settings)
    await fundamentals_db.ensure_table()
    monitor_task = asyncio.create_task(monitor.run_forever())
    price_history_task = asyncio.create_task(data_pipeline.run_daily_forever())
    try:
        yield
    finally:
        monitor_task.cancel()
        price_history_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
        with contextlib.suppress(asyncio.CancelledError):
            await price_history_task
        await price_history_db.close_pool()


app = FastAPI(
    title="Schwab FastAPI",
    version="0.1.0",
    contact={"name": "API"},
    lifespan=lifespan,
)

app.include_router(accounts.router)
app.include_router(auth.router)
app.include_router(orders.router)
app.include_router(dashboard.router)
app.include_router(line_webhook.router)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/accounts/login")


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "account": settings.schwab_account_id}
