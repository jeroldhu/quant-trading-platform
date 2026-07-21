"""ETF 横截面策略参数。"""

from pydantic import BaseModel, ConfigDict, Field


class CrossSectionalParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_n: int = Field(default=5, ge=1)
    max_position_weight: float = Field(default=0.2, gt=0, le=1)
