"""ETF 轮动策略骨架。"""

# TODO(P3-STRATEGY-01): 按锁定数据实现候选过滤、评分、择时与目标权重。
# Contract: docs/development-todo.md#p3-strategy-01

from quant_trading.config import StrategyFile
from quant_trading.models import TargetPosition
from quant_trading.research.strategy import StrategyContext

from .config import EtfRotationParameters


class EtfRotationStrategy:
    name = "etf_rotation"
    frequency = "weekly"

    def __init__(
        self,
        *,
        version: str,
        required_readiness: tuple[str, ...],
        parameters: EtfRotationParameters,
    ) -> None:
        self.version = version
        self.required_readiness = required_readiness
        self.parameters = parameters

    @classmethod
    def from_config(cls, config: StrategyFile) -> "EtfRotationStrategy":
        return cls(
            version=config.version,
            required_readiness=config.required_readiness,
            parameters=EtfRotationParameters.model_validate(config.parameters),
        )

    def generate_targets(
        self,
        context: StrategyContext,
    ) -> list[TargetPosition]:
        """按已计算分数排序；因子计算本身不放进注册表。"""

        ranked = sorted(
            (
                (instrument, context.factor_scores[instrument])
                for instrument in context.universe
                if instrument in context.factor_scores
            ),
            key=lambda item: (-item[1], item[0]),
        )[: self.parameters.top_n]
        if not ranked:
            return []

        weight = min(self.parameters.max_position_weight, 1.0 / len(ranked))
        if context.risk_state == "risk_off":
            weight *= 0.25
        return [
            TargetPosition(
                instrument_id=instrument,
                target_weight=weight,
                score=score,
                reason="ETF 轮动得分排名",
            )
            for instrument, score in ranked
        ]
