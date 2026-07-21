"""行情领域模型。"""

# TODO(P1-MODEL-01): 补齐 Gold 行情的来源、复权和质量审计字段。
# Contract: docs/development-todo.md#p1-model-01

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BarAdjustment(StrEnum):
    """价格口径；研究价格和真实成交价格不可混用。"""

    RAW = "raw"
    QFQ = "qfq"


class MarketBar(BaseModel):
    """一根日频 K 线。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    trade_date: date
    adjustment: BarAdjustment
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    amount: float = Field(ge=0)
    data_version: str

    @model_validator(mode="after")
    def validate_price_range(self) -> "MarketBar":
        """最高价和最低价必须包住开盘价与收盘价。"""

        if self.low > min(self.open, self.close):
            raise ValueError("low 不能高于 open 或 close")
        if self.high < max(self.open, self.close):
            raise ValueError("high 不能低于 open 或 close")
        return self
