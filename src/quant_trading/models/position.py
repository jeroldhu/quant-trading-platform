"""持仓与组合快照模型。"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class Position(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    quantity: int = Field(ge=0)
    average_raw_cost: float = Field(ge=0)


class PortfolioSnapshot(BaseModel):
    """某个交易日结束后的组合账本快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date
    cash: float = Field(ge=0)
    positions: tuple[Position, ...] = ()
    data_version: str
