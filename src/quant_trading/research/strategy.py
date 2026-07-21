"""所有策略共享的最小协议。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from quant_trading.models import MarketBar, TargetPosition


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """策略在某个历史时点可见的全部输入。"""

    trade_date: date
    data_version: str
    bars: tuple[MarketBar, ...]
    universe: tuple[str, ...]
    factor_scores: Mapping[str, float]
    risk_state: str = "risk_on"


class Strategy(Protocol):
    """仓库内所有正式策略必须满足的行为契约。"""

    name: str
    version: str
    frequency: str
    required_readiness: tuple[str, ...]

    def generate_targets(
        self,
        context: StrategyContext,
    ) -> list[TargetPosition]: ...
