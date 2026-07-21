"""主题轮动策略参数。"""

from pydantic import BaseModel, ConfigDict, Field


class ThemeRotationParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_n: int = Field(default=2, ge=1)
    max_position_weight: float = Field(default=0.3, gt=0, le=1)
