"""成交审计模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .order import OrderSide


class Trade(BaseModel):
    """使用真实未复权价格记录的一笔成交。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_id: str
    order_id: str
    instrument_id: str
    side: OrderSide
    quantity: int = Field(gt=0)
    raw_price: float = Field(gt=0)
    fee: float = Field(ge=0)
    executed_at: datetime
