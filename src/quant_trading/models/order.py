"""订单与撮合结果模型。

T 日 Signal → PendingOrder → T+1 Execution → Filled/Rejected/Expired。
成交价格必须为 raw，涨跌停/停牌导致 Rejected，超期未成交为 Expired。
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class RejectReason(StrEnum):
    LIMIT_UP_BLOCK = "limit_up_block"
    LIMIT_DOWN_BLOCK = "limit_down_block"
    SUSPENDED = "suspended"
    ZERO_VOLUME = "zero_volume"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_POSITION = "insufficient_position"
    LIMIT_PRICE = "limit_price"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"


class PendingOrder(BaseModel):
    """T 日创建、T+1 使用 raw 开盘价撮合的待执行订单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    signal_id: str
    instrument_id: str
    asset_type: Literal["etf", "stock"]
    side: OrderSide
    target_quantity: int = Field(gt=0)
    limit_price: float | None = Field(
        default=None,
        gt=0,
        description="买入不高于此价、卖出不低于此价；None 表示不限价",
    )
    ttl: int = Field(default=1, ge=1, description="存活交易日数")
    created_at: datetime
    execution_date: date


class Execution(BaseModel):
    """订单撮合终态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    status: ExecutionStatus
    filled_quantity: int = Field(ge=0)
    remaining_quantity: int = Field(ge=0)
    filled_price: float = Field(ge=0)
    fee: float = Field(ge=0)
    reject_reason: str | None = Field(
        default=None,
        description="rejected/expired 时必填原因",
    )

    @model_validator(mode="after")
    def _check_reject_reason(self) -> "Execution":
        if (
            self.status in (ExecutionStatus.REJECTED, ExecutionStatus.EXPIRED)
            and not self.reject_reason
        ):
            raise ValueError(f"{self.status} 时必须填写 reject_reason")
        return self
