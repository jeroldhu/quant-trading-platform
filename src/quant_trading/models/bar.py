"""行情领域模型。

Gold 行情的完整键为 (instrument_id, trade_date, adjustment, data_version)。
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BarAdjustment(StrEnum):
    RAW = "raw"
    QFQ = "qfq"


class TradingStatus(StrEnum):
    NORMAL = "NORMAL"
    SUSPENDED = "SUSPENDED"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"


class MarketBar(BaseModel):
    """一根日频 K 线——覆盖 Bronze/Silver/Gold 共有字段。

    Bronze/Silver 额外携带 source_id/raw_hash/quality_status，
    这些字段在 Gold 中由 source_count/source_map/quality_flags 替代。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- 业务主键 -------------------------------------------------
    instrument_id: str
    trade_date: date
    adjustment: BarAdjustment
    data_version: str

    # -- OHLCV ---------------------------------------------------
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    amount: float = Field(ge=0)

    # -- 行情状态 -------------------------------------------------
    pre_close: float | None = Field(default=None, gt=0)
    turnover_rate: float | None = Field(default=None, ge=0)
    trading_status: TradingStatus = TradingStatus.NORMAL
    up_limit: float | None = Field(default=None, gt=0)
    down_limit: float | None = Field(default=None, gt=0)
    is_st: bool = False

    # -- Gold 来源审计 --------------------------------------------
    source_count: int = Field(default=0, ge=0)
    source_map: dict[str, bool] = Field(default_factory=dict)
    quality_flags: tuple[str, ...] = ()
    amount_source_count: int = Field(default=0, ge=0)

    # -- 复权追溯 -------------------------------------------------
    adjustment_source: str | None = None
    adjustment_date: date | None = None
    adjustment_version: str | None = None

    # -- 发布元数据 -----------------------------------------------
    published_at: datetime | None = None
    schema_version: str = "1.0.0"

    @model_validator(mode="after")
    def _validate_price_range(self) -> "MarketBar":
        if self.low > min(self.open, self.close):
            raise ValueError("low 不能高于 open 或 close")
        if self.high < max(self.open, self.close):
            raise ValueError("high 不能低于 open 或 close")
        return self

    @model_validator(mode="after")
    def _validate_pre_close(self) -> "MarketBar":
        if self.pre_close is not None and self.pre_close <= 0:
            raise ValueError("pre_close 必须为正")
        return self
