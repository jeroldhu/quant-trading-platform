"""成交审计模型。

每一笔成交必须可分解为佣金、印花税、滑点等明细项。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    # -- 费用明细 ----------------------------------------------
    commission: float = Field(ge=0, description="按费率计算的佣金")
    minimum_commission: float = Field(ge=0, description="最低佣金补足额")
    stamp_duty: float = Field(ge=0, description="印花税（卖出收取，ETF 为 0）")
    slippage: float = Field(ge=0, description="滑点成本")
    total_fee: float = Field(ge=0)

    executed_at: datetime

    @model_validator(mode="after")
    def _check_fee_breakdown(self) -> "Trade":
        expected = (
            self.commission + self.minimum_commission + self.stamp_duty + self.slippage
        )
        if abs(expected - self.total_fee) > 0.01:
            raise ValueError(
                f"费用分项之和({expected:.4f})与 total_fee({self.total_fee:.4f}) 不一致"
            )
        return self
