from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PriceHistoryQuery(BaseModel):
    periodType: Literal["day", "month", "year", "ytd"] | None = None
    period: Optional[int] = Field(None, gt=0)
    frequencyType: Literal["minute", "daily", "weekly", "monthly"] | None = None
    frequency: Optional[int] = Field(None, gt=0)
    startDate: Optional[int | datetime] = Field(None, description="Start epoch milliseconds or ISO datetime")
    endDate: Optional[int | datetime] = Field(None, description="End epoch milliseconds or ISO datetime")
    needExtendedHoursData: Optional[bool] = None
    needPreviousClose: Optional[bool] = None

    class Config:
        extra = "forbid"
