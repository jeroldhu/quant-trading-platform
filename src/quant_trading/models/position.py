"""持仓与组合快照模型。"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Position(BaseModel):
    """单只证券的持仓。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    available_quantity: int = Field(ge=0, description="可卖数量")
    frozen_quantity: int = Field(ge=0, description="冻结数量（T+1 交收中）")
    average_raw_cost: float = Field(ge=0, description="加权平均成本（raw）")
    market_value: float = Field(ge=0, description="以 raw 收盘价计算的市值")

    @model_validator(mode="after")
    def _total_quantity_positive(self) -> "Position":
        if self.available_quantity + self.frozen_quantity <= 0:
            raise ValueError("持仓必须有正数量")
        return self


class PortfolioSnapshot(BaseModel):
    """某个交易日收盘后的组合账本快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date
    cash: float = Field(ge=0)
    frozen_cash: float = Field(ge=0, description="T+1 交收冻结现金")
    positions: tuple[Position, ...] = ()
    total_market_value: float = Field(ge=0)
    net_value: float = Field(ge=0, description="现金 + 冻结现金 + 持仓市值")
    data_version: str

    @model_validator(mode="after")
    def _check_ledger_balance(self) -> "PortfolioSnapshot":
        positions_value = sum(p.market_value for p in self.positions)
        expected_net = self.cash + self.frozen_cash + positions_value
        if abs(expected_net - self.net_value) > 0.01:
            raise ValueError(
                f"账本不平衡: 现金({self.cash:.2f}) + 冻结({self.frozen_cash:.2f})"
                f" + 持仓({positions_value:.2f}) ≠ 净值({self.net_value:.2f})"
            )
        return self
