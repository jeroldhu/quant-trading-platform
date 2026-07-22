"""多策略目标仓位合并——预算、集中度、现金下限、risk-off 归因。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from quant_trading.models import TargetPosition


@dataclass
class AllocationConstraints:
    total_budget: float = 0.80
    min_cash: float = 0.20
    max_single_weight: float = 0.50
    max_positions: int = 10
    max_concentration: int = 2
    risk_off_multiplier: float = 0.25
    strategy_priority: tuple[str, ...] = ()
    concentration_group_by_instrument: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.min_cash <= 1:
            raise ValueError("min_cash 必须在 [0, 1]")
        if not 0 <= self.total_budget <= 1 - self.min_cash:
            raise ValueError("total_budget 不能超过 1 - min_cash")
        if not 0 < self.max_single_weight <= 1:
            raise ValueError("max_single_weight 必须在 (0, 1]")
        if self.max_positions < 1:
            raise ValueError("max_positions 必须 >= 1")
        if self.max_concentration < 1:
            raise ValueError("max_concentration 必须 >= 1")
        if not 0 <= self.risk_off_multiplier <= 1:
            raise ValueError("risk_off_multiplier 必须在 [0, 1]")


@dataclass
class AllocationResult:
    positions: dict[str, float] = field(default_factory=dict)
    adjustments: list[str] = field(default_factory=list)
    attribution: dict[str, dict[str, float]] = field(default_factory=dict)


def merge_targets(
    strategy_targets: Mapping[str, Sequence[TargetPosition]],
    capital_weights: Mapping[str, float],
) -> dict[str, float]:
    """按策略资金权重合并目标。"""
    merged: dict[str, float] = {}
    if sum(capital_weights.values()) > 1.0 + 1e-12:
        raise ValueError("策略资金权重合计不能超过 1")
    for strat_name, targets in strategy_targets.items():
        cw = capital_weights.get(strat_name, 0.0)
        for tp in targets:
            merged[tp.instrument_id] = (
                merged.get(tp.instrument_id, 0.0) + tp.target_weight * cw
            )
    return merged


def allocate(
    strategy_targets: Mapping[str, Sequence[TargetPosition]],
    capital_weights: Mapping[str, float],
    constraints: AllocationConstraints,
    risk_state: str = "risk_on",
) -> AllocationResult:
    """全约束组合分配——预算、集中度、risk-off 缩放、归因追踪。"""

    raw = merge_targets(strategy_targets, capital_weights)
    versions = {
        (target.signal_date, target.data_version)
        for targets in strategy_targets.values()
        for target in targets
    }
    if len(versions) > 1:
        raise ValueError("不能合并不同信号日期或 data_version 的目标仓位")
    attribution: dict[str, dict[str, float]] = {}
    for strat_name, targets in strategy_targets.items():
        for tp in targets:
            attribution.setdefault(strat_name, {})[tp.instrument_id] = (
                tp.target_weight * capital_weights.get(strat_name, 0.0)
            )

    adjustments: list[str] = []
    result: dict[str, float] = {}
    total = 0.0

    priority = {
        strategy_name: index
        for index, strategy_name in enumerate(constraints.strategy_priority)
    }

    def allocation_order(item: tuple[str, float]) -> tuple[int, float, str]:
        instrument, weight = item
        owners = [
            strategy_name
            for strategy_name, values in attribution.items()
            if instrument in values
        ]
        owner_priority = min(
            (priority.get(owner, len(priority)) for owner in owners),
            default=len(priority),
        )
        return owner_priority, -weight, instrument

    concentration_counts: dict[str, int] = {}
    # 先按显式策略优先级，再按权重降序分配。
    for inst, weight in sorted(raw.items(), key=allocation_order):
        if len(result) >= constraints.max_positions:
            adjustments.append(f"{inst}: 超过最大持仓数 {constraints.max_positions}")
            continue
        group = constraints.concentration_group_by_instrument.get(inst)
        if group is not None and concentration_counts.get(group, 0) >= (
            constraints.max_concentration
        ):
            adjustments.append(
                f"{inst}: 集中度组 {group} 已达到 {constraints.max_concentration} 只"
            )
            continue
        capped = min(weight, constraints.max_single_weight)
        if capped < weight:
            adjustments.append(
                f"{inst}: 权重从 {weight:.4f} 削减为 {capped:.4f}（单标的上限）"
            )
        if total + capped > constraints.total_budget:
            capped = max(0.0, constraints.total_budget - total)
            if capped > 0:
                adjustments.append(
                    f"{inst}: 权重从 {weight:.4f} 削减为 {capped:.4f}（budget）"
                )
        if capped <= 0:
            if weight > 0:
                adjustments.append(f"{inst}: 预算已满，无法分配")
            continue
        result[inst] = capped
        total += capped
        if group is not None:
            concentration_counts[group] = concentration_counts.get(group, 0) + 1

    # risk_off 缩放
    if risk_state == "risk_off":
        for inst in list(result):
            result[inst] *= constraints.risk_off_multiplier
        adjustments.append(f"risk_off: 全部权重 × {constraints.risk_off_multiplier}")

    result = {instrument: weight for instrument, weight in result.items() if weight > 0}

    return AllocationResult(
        positions=result,
        adjustments=adjustments,
        attribution=attribution,
    )
