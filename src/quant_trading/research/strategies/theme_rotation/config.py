"""主题轮动策略参数。"""

# TODO(P3-STRATEGY-02): 补齐主题评分、主题内选股和集中度参数校验。
# Contract: docs/development-todo.md#p3-strategy-02

from pydantic import BaseModel, ConfigDict, Field


class ThemeRotationParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_n: int = Field(default=2, ge=1)
    max_position_weight: float = Field(default=0.3, gt=0, le=1)
