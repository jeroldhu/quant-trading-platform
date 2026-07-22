"""策略信号与目标仓位模型。

每一条 Signal/TargetPosition 必须可追溯到策略版本、数据版本和输入配置。
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Signal(BaseModel):
    """策略在 T 日收盘后产生的可审计信号。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str
    strategy_name: str
    strategy_version: str
    signal_date: date
    execution_date: date
    instrument_id: str
    asset_type: Literal["etf", "stock"] = "etf"
    score: float
    data_version: str

    config_hash: str = Field(
        default="",
        description="策略配置文件的 SHA-256，保证信号可复现",
    )
    factor_attribution: dict[str, float] = Field(
        default_factory=dict,
        description="因子名 → 贡献值，解释信号来源",
    )
    generated_at: datetime | None = None


class TargetPosition(BaseModel):
    """策略希望持有的目标权重，不代表订单已成交。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    asset_type: Literal["etf", "stock"] = "etf"
    signal_date: date
    strategy_name: str
    strategy_version: str
    data_version: str
    config_hash: str
    target_weight: float = Field(ge=0.0, le=1.0)
    score: float
    reason: str = ""
