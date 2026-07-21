"""策略信号与目标仓位模型。"""

# TODO(P1-MODEL-02): 增加稳定 ID、执行日期、配置哈希和因子归因。
# Contract: docs/development-todo.md#p1-model-02

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class Signal(BaseModel):
    """策略在 T 日收盘后产生的可审计信号。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_name: str
    strategy_version: str
    signal_date: date
    instrument_id: str
    score: float
    data_version: str


class TargetPosition(BaseModel):
    """策略希望持有的目标权重，不代表订单已经成交。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    target_weight: float = Field(ge=0.0, le=1.0)
    score: float
    reason: str = ""
