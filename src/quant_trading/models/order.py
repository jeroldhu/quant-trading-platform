"""订单与撮合结果模型。"""

# TODO(P1-MODEL-03): 完成 T+1 订单状态机、TTL 和拒绝原因约束。
# Contract: docs/development-todo.md#p1-model-03

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PendingOrder(BaseModel):
    """T 日创建、T+1 使用 raw 价格撮合的待执行订单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    signal_id: str
    instrument_id: str
    side: OrderSide
    target_quantity: int = Field(gt=0)
    created_at: datetime
    execution_date: date


class Execution(BaseModel):
    """订单撮合终态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    status: ExecutionStatus
    filled_quantity: int = Field(ge=0)
    filled_price: float = Field(ge=0)
    fee: float = Field(ge=0)
    reject_reason: str | None = None
