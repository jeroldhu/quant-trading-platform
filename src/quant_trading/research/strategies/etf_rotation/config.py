"""ETF 轮动策略参数。"""

from pydantic import BaseModel, ConfigDict, Field


class EtfRotationParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_n: int = Field(default=2, ge=1, le=2)
    lookback_days: int = Field(default=20, ge=2)
    max_position_weight: float = Field(default=0.4, gt=0, le=1)
