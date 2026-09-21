from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class Instrument(BaseModel):
    symbol: str
    assetType: str = "EQUITY"


class OrderLeg(BaseModel):
    instruction: str
    quantity: float
    instrument: Instrument


class OrderRequest(BaseModel):
    orderType: str
    session: str = "NORMAL"
    duration: str = "DAY"
    orderStrategyType: str = "SINGLE"
    price: Optional[float] = None
    stopPrice: Optional[float] = None
    orderLegCollection: list[OrderLeg]
