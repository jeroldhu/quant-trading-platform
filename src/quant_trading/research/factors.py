"""跨策略复用的纯因子函数。"""

# TODO(P3-RESEARCH-02): 实现文档定义的 qfq 动量、强弱、波动率和回撤因子。
# Contract: docs/development-todo.md#p3-research-02

from collections.abc import Sequence


def total_return(closes: Sequence[float]) -> float | None:
    """计算区间收益；调用方必须传入 qfq 收盘价。"""

    if len(closes) < 2 or closes[0] <= 0:
        return None
    return closes[-1] / closes[0] - 1.0
