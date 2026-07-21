"""主题轮动策略骨架。"""

# TODO(P3-STRATEGY-02): 实现 point-in-time 主题成员、评分和主题内选股。
# Contract: docs/development-todo.md#p3-strategy-02

from quant_trading.config import StrategyFile
from quant_trading.models import TargetPosition
from quant_trading.research.strategy import StrategyContext

from .config import ThemeRotationParameters


class ThemeRotationStrategy:
    name = "theme_rotation"
    frequency = "weekly"

    def __init__(
        self,
        *,
        version: str,
        required_readiness: tuple[str, ...],
        parameters: ThemeRotationParameters,
    ) -> None:
        self.version = version
        self.required_readiness = required_readiness
        self.parameters = parameters

    @classmethod
    def from_config(cls, config: StrategyFile) -> "ThemeRotationStrategy":
        return cls(
            version=config.version,
            required_readiness=config.required_readiness,
            parameters=ThemeRotationParameters.model_validate(config.parameters),
        )

    def generate_targets(
        self,
        context: StrategyContext,
    ) -> list[TargetPosition]:
        """暂以外部提供的主题得分生成目标，后续接入主题成员解析。"""

        ranked = sorted(
            context.factor_scores.items(),
            key=lambda item: (-item[1], item[0]),
        )[: self.parameters.top_n]
        return [
            TargetPosition(
                instrument_id=instrument,
                target_weight=self.parameters.max_position_weight,
                score=score,
                reason="主题轮动得分排名",
            )
            for instrument, score in ranked
        ]
