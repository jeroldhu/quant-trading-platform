"""多策略目标仓位合并。"""

# TODO(P3-RESEARCH-05): 实现预算、集中度、现金下限、risk-off 和完整归因。
# Contract: docs/development-todo.md#p3-research-05

from collections.abc import Mapping, Sequence

from quant_trading.models import TargetPosition


def merge_targets(
    strategy_targets: Mapping[str, Sequence[TargetPosition]],
    capital_weights: Mapping[str, float],
) -> dict[str, float]:
    """按策略资金权重合并目标；超限约束将在真实组合模块中补充。"""

    merged: dict[str, float] = {}
    for strategy_name, targets in strategy_targets.items():
        capital_weight = capital_weights[strategy_name]
        for target in targets:
            merged[target.instrument_id] = merged.get(target.instrument_id, 0.0) + (
                target.target_weight * capital_weight
            )
    return merged
