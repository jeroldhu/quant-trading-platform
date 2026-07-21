"""ETF 轮动策略参数。"""

# TODO(P3-STRATEGY-01): 补齐 ETF 轮动因子、风险开关和权重参数校验。
# Contract: docs/development-todo.md#p3-strategy-01

from pydantic import BaseModel, ConfigDict, Field


class EtfRotationParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_n: int = Field(default=2, ge=1)
    lookback_days: int = Field(default=20, ge=2)
    max_position_weight: float = Field(default=0.4, gt=0, le=1)
