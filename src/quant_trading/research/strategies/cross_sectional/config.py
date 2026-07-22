"""ETF 横截面策略参数。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

FACTOR_NAMES = {
    "ret_20d",
    "ret_60d",
    "relative_strength_20d",
    "relative_strength_60d",
    "ma20_distance",
    "amount_ratio_5_20",
    "volatility_20d",
    "drawdown_60d",
}


class CrossSectionalParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_n: int = Field(default=5, ge=1, le=10)
    max_position_weight: float = Field(default=0.20, gt=0, le=1)
    min_history_bars: int = Field(default=252, ge=61)
    min_avg_amount_20d: float = Field(default=100_000_000, gt=0)
    factor_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "ret_20d": 0.25,
            "ret_60d": 0.20,
            "relative_strength_20d": 0.15,
            "relative_strength_60d": 0.10,
            "ma20_distance": 0.10,
            "amount_ratio_5_20": 0.05,
            "volatility_20d": -0.10,
            "drawdown_60d": -0.05,
        }
    )

    @model_validator(mode="after")
    def _validate_factor_weights(self) -> "CrossSectionalParameters":
        if set(self.factor_weights) != FACTOR_NAMES:
            missing = FACTOR_NAMES - set(self.factor_weights)
            extra = set(self.factor_weights) - FACTOR_NAMES
            raise ValueError(f"因子权重键不完整: missing={missing}, extra={extra}")
        absolute_total = sum(abs(value) for value in self.factor_weights.values())
        if abs(absolute_total - 1.0) > 1e-9:
            raise ValueError("factor_weights 绝对值合计必须为 1")
        return self
