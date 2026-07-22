"""ETF 横截面多因子策略——全市场过滤、因子标准化、加权评分。"""

from datetime import date

from quant_trading.config import StrategyFile
from quant_trading.models import TargetPosition
from quant_trading.research.factors import (
    amount_ratio,
    ma_distance,
    max_drawdown,
    period_return,
    realized_volatility,
    relative_strength,
)
from quant_trading.research.strategy import StrategyContext

from .config import CrossSectionalParameters


class CrossSectionalStrategy:
    name = "cross_sectional"
    frequency = "weekly"
    asset_type = "etf"

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

    def _compute_scores(
        self,
        context: StrategyContext,
    ) -> dict[str, float]:
        by_inst: dict[str, list[tuple[date, float, float]]] = {}
        for bar in context.bars:
            if bar.instrument_id in context.universe:
                by_inst.setdefault(bar.instrument_id, []).append(
                    (bar.trade_date, bar.close, bar.amount)
                )

        bench: list[float] = [b.close for b in context.benchmark_bars]

        factor_rows: dict[str, dict[str, float]] = {}
        for inst in context.universe:
            ordered = sorted(by_inst.get(inst, []), key=lambda item: item[0])
            closes = [item[1] for item in ordered]
            amounts = [item[2] for item in ordered]
            if len(closes) < self.parameters.min_history_bars:
                continue
            if sum(amounts[-20:]) / 20 < self.parameters.min_avg_amount_20d:
                continue

            ret20 = period_return(closes, 20)
            ret60 = period_return(closes, 60)
            vol20 = realized_volatility(closes[-21:])
            dd60 = max_drawdown(closes[-60:])
            ma_dist = ma_distance(closes[-20:])
            rs20 = relative_strength(closes, bench, window=20)
            rs60 = relative_strength(closes, bench, window=60)
            amt_ratio = amount_ratio(amounts) if len(amounts) >= 20 else None

            fv = {
                "ret_20d": ret20,
                "ret_60d": ret60,
                "relative_strength_20d": rs20,
                "relative_strength_60d": rs60,
                "ma20_distance": ma_dist,
                "amount_ratio_5_20": amt_ratio,
                "volatility_20d": vol20,
                "drawdown_60d": dd60,
            }
            if any(value is None for value in fv.values()):
                continue

            factor_rows[inst] = {
                key: value for key, value in fv.items() if value is not None
            }

        standardized: dict[str, dict[str, float]] = {
            instrument: {} for instrument in factor_rows
        }
        for factor_name in self.parameters.factor_weights:
            values = [row[factor_name] for row in factor_rows.values()]
            if not values:
                continue
            mean = sum(values) / len(values)
            variance = sum((value - mean) ** 2 for value in values) / len(values)
            scale = variance**0.5
            for instrument, row in factor_rows.items():
                standardized[instrument][factor_name] = (
                    (row[factor_name] - mean) / scale if scale > 0 else 0.0
                )

        scores: dict[str, float] = {}
        for instrument, factors in standardized.items():
            scores[instrument] = sum(
                self.parameters.factor_weights[name] * factors[name]
                for name in self.parameters.factor_weights
            )

        return scores

    def generate_targets(
        self,
        context: StrategyContext,
    ) -> list[TargetPosition]:
        if not context.universe:
            return []

        scores = self._compute_scores(context)
        ranked = sorted(scores.items(), key=lambda x: -x[1])[: self.parameters.top_n]
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
                reason=f"横截面得分 {score:.4f}",
            )
            for inst, score in ranked
        ]
