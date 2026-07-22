"""策略共享协议、上下文与运行器。

运行器先检查 required_readiness，再调用策略；任一门禁失败则阻断。
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from quant_trading.data.readiness import (
    ReadinessRegistry,
    ReadinessStatus,
    require_readiness,
)
from quant_trading.models import MarketBar, Signal, TargetPosition


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """策略在某个历史时点可见的全部输入。"""

    trade_date: date
    data_version: str
    snapshot_id: str = ""
    bars: tuple[MarketBar, ...] = ()
    benchmark_bars: tuple[MarketBar, ...] = ()
    calendar: tuple[date, ...] = ()
    universe: tuple[str, ...] = ()
    theme_members: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    readiness: Mapping[str, ReadinessStatus] = field(default_factory=dict)
    factor_scores: Mapping[str, float] = field(default_factory=dict)
    risk_state: str = "risk_on"
    config_hash: str = ""


class Strategy(Protocol):
    """所有正式策略必须满足的行为契约。"""

    name: str
    version: str
    frequency: str
    asset_type: str
    required_readiness: tuple[str, ...]

    def generate_targets(self, context: StrategyContext) -> list[TargetPosition]: ...


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


class StrategyRunner:
    """加载启用策略，按门禁 → 因子 → 信号严格执行。"""

    def __init__(
        self,
        strategies: Mapping[str, Strategy],
        registry: ReadinessRegistry,
    ) -> None:
        self._strategies = dict(strategies)
        self._registry = registry

    def run_single(
        self,
        strategy: Strategy,
        context: StrategyContext,
    ) -> list[TargetPosition]:
        """运行单个策略，前置门禁检查。"""
        if not context.data_version or not context.snapshot_id:
            raise ValueError("正式策略运行必须锁定 data_version 和 snapshot_id")
        if not context.config_hash:
            raise ValueError("正式策略运行必须记录 config_hash")
        if any(bar.data_version != context.data_version for bar in context.bars):
            raise ValueError("策略输入包含未锁定版本")
        if any(bar.adjustment.value != "qfq" for bar in context.bars):
            raise ValueError("策略因子与信号只能读取 qfq 行情")
        for gate_name in strategy.required_readiness:
            from quant_trading.data.readiness import ReadinessGate

            gate = ReadinessGate(gate_name)
            status = context.readiness.get(gate_name) or self._registry.get(
                gate, context.trade_date, context.data_version
            )
            require_readiness(status)
        return strategy.generate_targets(context)

    def run_all(
        self,
        contexts: Mapping[str, StrategyContext],
    ) -> dict[str, list[TargetPosition]]:
        """运行所有注册策略，返回 strategy_name → targets。"""
        results: dict[str, list[TargetPosition]] = {}
        for name, strategy in self._strategies.items():
            ctx = contexts.get(name)
            if ctx is None:
                continue
            results[name] = self.run_single(strategy, ctx)
        return results


def targets_to_signals(
    targets: list[TargetPosition], *, execution_date: date
) -> list[Signal]:
    """把可追溯目标仓位转换为稳定 ID 的 T+1 信号。"""

    signals: list[Signal] = []
    for target in targets:
        if execution_date <= target.signal_date:
            raise ValueError("execution_date 必须晚于 signal_date")
        identity = "|".join(
            (
                target.strategy_name,
                target.strategy_version,
                target.signal_date.isoformat(),
                target.instrument_id,
                target.data_version,
                target.config_hash,
            )
        )
        signals.append(
            Signal(
                signal_id="SIG-" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                strategy_name=target.strategy_name,
                strategy_version=target.strategy_version,
                signal_date=target.signal_date,
                execution_date=execution_date,
                instrument_id=target.instrument_id,
                asset_type=target.asset_type,
                score=target.score,
                data_version=target.data_version,
                config_hash=target.config_hash,
                factor_attribution={},
            )
        )
    return signals
