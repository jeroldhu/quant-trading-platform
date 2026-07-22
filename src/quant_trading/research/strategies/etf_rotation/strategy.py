"""ETF 轮动策略——从候选池选最多 top_n 只得分最高 ETF。"""

from datetime import date

from quant_trading.config import StrategyFile
from quant_trading.models import TargetPosition
from quant_trading.research.factors import (
    ma_distance,
    max_drawdown,
    period_return,
    realized_volatility,
)
from quant_trading.research.strategy import StrategyContext

from .config import EtfRotationParameters

FACTOR_WEIGHTS = {
    "ret_20d": 0.30,
    "ret_60d": 0.25,
    "ma20_distance": 0.20,
    "volatility_20d": -0.15,
    "drawdown_60d": 0.10,
}


class EtfRotationStrategy:
    name = "etf_rotation"
    frequency = "weekly"
    asset_type = "etf"

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

    def _compute_scores(
        self,
        context: StrategyContext,
    ) -> dict[str, float]:
        """用 qfq 序列计算各候选 ETF 的因子得分。"""
        by_inst: dict[str, list[tuple[date, float]]] = {}
        for bar in context.bars:
            if bar.instrument_id in context.universe:
                by_inst.setdefault(bar.instrument_id, []).append(
                    (bar.trade_date, bar.close)
                )

        bench_closes: list[float] = []
        for bar in context.benchmark_bars:
            bench_closes.append(bar.close)

        scores: dict[str, float] = {}
        for inst in context.universe:
            closes = [
                close
                for _, close in sorted(by_inst.get(inst, []), key=lambda item: item[0])
            ]
            lookback = self.parameters.lookback_days

            if len(closes) < 61:
                continue

            ret20 = period_return(closes, lookback)
            ret60 = period_return(closes, 60)
            vol20 = realized_volatility(closes[-(lookback + 1) :])
            ma_dist = ma_distance(closes[-min(lookback, len(closes)) :])
            dd60 = max_drawdown(closes[-min(60, len(closes)) :])

            factor_vals = {
                "ret_20d": ret20,
                "ret_60d": ret60,
                "ma20_distance": ma_dist,
                "volatility_20d": vol20,
                "drawdown_60d": dd60,
            }
            if None in factor_vals.values():
                continue
            if (ret20 or 0.0) <= 0 or (ret60 or 0.0) <= 0:
                continue

            score = sum(
                FACTOR_WEIGHTS[k] * (factor_vals[k] or 0) for k in FACTOR_WEIGHTS
            )
            scores[inst] = score

        return scores

    def generate_targets(
        self,
        context: StrategyContext,
    ) -> list[TargetPosition]:
        if not context.universe:
            return []

        scores = self._compute_scores(context)
        if not scores:
            return []

        ranked = sorted(scores.items(), key=lambda x: -x[1])[: self.parameters.top_n]
        ranked = [item for item in ranked if item[1] > 0]
        if not ranked:
            return []
        weight = min(self.parameters.max_position_weight, 1.0 / len(ranked))

        return [
            TargetPosition(
                instrument_id=inst,
                signal_date=context.trade_date,
                strategy_name=self.name,
                strategy_version=self.version,
                data_version=context.data_version,
                config_hash=context.config_hash,
                target_weight=weight,
                score=score,
                reason=f"ETF 轮动得分 {score:.4f}",
            )
            for inst, score in ranked
        ]
