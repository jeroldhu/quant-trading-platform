"""主题轮动策略参数。"""

from pydantic import BaseModel, ConfigDict, Field


class ThemeRotationParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_themes: int = Field(default=2, ge=1, le=10)
    stocks_per_theme: int = Field(default=3, ge=1, le=10)
    min_history_bars: int = Field(default=61, ge=21)
    max_position_weight: float = Field(default=0.15, gt=0, le=1)
    min_avg_amount_20d: float = Field(default=50_000_000, gt=0)
