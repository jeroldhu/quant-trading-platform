"""主题轮动策略——先选择强势主题，再从历史成分快照内选择股票。"""

from collections import defaultdict
from datetime import date

from quant_trading.config import StrategyFile
from quant_trading.models import TargetPosition
from quant_trading.research.factors import period_return, realized_volatility
from quant_trading.research.strategy import StrategyContext

from .config import ThemeRotationParameters


class ThemeRotationStrategy:
    name = "theme_rotation"
    frequency = "weekly"
    asset_type = "stock"

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
        if not context.universe or not context.theme_members:
            return []

        histories: dict[str, list[tuple[date, float, float, bool]]] = defaultdict(list)
        for bar in context.bars:
            histories[bar.instrument_id].append(
                (bar.trade_date, bar.close, bar.amount, bar.is_st)
            )

        stock_scores: dict[str, float] = {}
        for instrument in context.universe:
            ordered = sorted(histories.get(instrument, ()), key=lambda item: item[0])
            if len(ordered) < self.parameters.min_history_bars:
                continue
            if ordered[-1][3]:
                continue
            closes = [item[1] for item in ordered]
            amounts = [item[2] for item in ordered]
            if sum(amounts[-20:]) / 20 < self.parameters.min_avg_amount_20d:
                continue
            ret20 = period_return(closes, 20)
            ret60 = period_return(closes, 60)
            volatility = realized_volatility(closes[-21:])
            if ret20 is None or ret60 is None or volatility is None:
                continue
            # 可解释的绝对动量分数；负动量股票不进入正式目标。
            score = 0.45 * ret20 + 0.45 * ret60 - 0.10 * volatility
            if ret20 > 0 and ret60 > 0 and score > 0:
                stock_scores[instrument] = score

        theme_scores: list[tuple[str, float]] = []
        for theme, members in context.theme_members.items():
            available = sorted(
                (stock_scores[item] for item in members if item in stock_scores),
                reverse=True,
            )
            if available:
                sample = available[: self.parameters.stocks_per_theme]
                theme_scores.append((theme, sum(sample) / len(sample)))
        selected_themes = sorted(theme_scores, key=lambda item: (-item[1], item[0]))[
            : self.parameters.top_themes
        ]
        if not selected_themes:
            return []

        selected: list[tuple[str, float, str]] = []
        used: set[str] = set()
        for theme, _ in selected_themes:
            ranked = sorted(
                (
                    (instrument, stock_scores[instrument])
                    for instrument in context.theme_members[theme]
                    if instrument in stock_scores and instrument not in used
                ),
                key=lambda item: (-item[1], item[0]),
            )[: self.parameters.stocks_per_theme]
            for instrument, score in ranked:
                used.add(instrument)
                selected.append((instrument, score, theme))
        if not selected:
            return []
        weight = min(self.parameters.max_position_weight, 1.0 / len(selected))
        return [
            TargetPosition(
                instrument_id=inst,
                asset_type="stock",
                signal_date=context.trade_date,
                strategy_name=self.name,
                strategy_version=self.version,
                data_version=context.data_version,
                config_hash=context.config_hash,
                target_weight=weight,
                score=score,
                reason=f"{theme} 主题内选股得分 {score:.4f}",
            )
            for inst, score, theme in selected
        ]
