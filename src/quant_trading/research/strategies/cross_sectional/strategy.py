"""ETF 横截面策略骨架。"""

from quant_trading.config import StrategyFile
from quant_trading.models import TargetPosition
from quant_trading.research.strategy import StrategyContext

from .config import CrossSectionalParameters


class CrossSectionalStrategy:
    name = "cross_sectional"
    frequency = "weekly"

    def __init__(
        self,
        *,
        version: str,
        required_readiness: tuple[str, ...],
        parameters: CrossSectionalParameters,
    ) -> None:
        self.version = version
        self.required_readiness = required_readiness
        self.parameters = parameters

    @classmethod
    def from_config(cls, config: StrategyFile) -> "CrossSectionalStrategy":
        return cls(
            version=config.version,
            required_readiness=config.required_readiness,
            parameters=CrossSectionalParameters.model_validate(config.parameters),
        )

    def generate_targets(
        self,
        context: StrategyContext,
    ) -> list[TargetPosition]:
        ranked = sorted(
            (
                (instrument, context.factor_scores[instrument])
                for instrument in context.universe
                if instrument in context.factor_scores
            ),
            key=lambda item: (-item[1], item[0]),
        )[: self.parameters.top_n]
        return [
            TargetPosition(
                instrument_id=instrument,
                target_weight=self.parameters.max_position_weight,
                score=score,
                reason="横截面综合得分排名",
            )
            for instrument, score in ranked
        ]
