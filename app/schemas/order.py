from typing import Literal, Optional

from pydantic import BaseModel, Field


class OrderLeg(BaseModel):
    instruction: Literal["BUY", "SELL", "BUY_TO_COVER", "SELL_SHORT"]
    quantity: float = Field(..., gt=0)
    symbol: str
    assetType: Literal["EQUITY", "OPTION", "MUTUAL_FUND", "CASH_EQUIVALENT"]


class OrderRequest(BaseModel):
    orderType: str = "MARKET"
    session: str = "NORMAL"
    duration: str = "DAY"
    orderStrategyType: str = "SINGLE"
    price: Optional[float] = None
    orderLegCollection: list[OrderLeg]
