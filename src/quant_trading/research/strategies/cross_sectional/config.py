"""ETF 横截面策略参数。"""

# TODO(P3-STRATEGY-03): 补齐全市场过滤、因子权重和 Top-N 参数校验。
# Contract: docs/development-todo.md#p3-strategy-03

from pydantic import BaseModel, ConfigDict, Field


class CrossSectionalParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_n: int = Field(default=5, ge=1)
    max_position_weight: float = Field(default=0.2, gt=0, le=1)
