"""跨策略复用的纯因子函数。

只使用锁定版本的 qfq 序列；历史不足返回 None 而非补零。
"""

from collections.abc import Sequence
from math import sqrt


def total_return(closes: Sequence[float]) -> float | None:
    """区间累计收益。"""
    if len(closes) < 2 or closes[0] <= 0:
        return None
    result = float(closes[-1] / closes[0] - 1.0)
    return result


def period_return(closes: Sequence[float], periods: int) -> float | None:
    """计算严格 N 个交易期间收益，需要至少 N+1 个收盘价。"""
    if periods < 1:
        raise ValueError("periods 必须 >= 1")
    if len(closes) < periods + 1:
        return None
    return total_return(closes[-(periods + 1) :])


def annualized_return(closes: Sequence[float], trading_days: int = 252) -> float | None:
    """年化收益。"""
    r = total_return(closes)
    if r is None:
        return None
    n = len(closes)
    if n < 2:
        return None
    return float((1 + r) ** (trading_days / (n - 1)) - 1.0)


def realized_volatility(closes: Sequence[float]) -> float | None:
    """年化波动率（基于日收益）。"""
    if len(closes) < 3:
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return sqrt(var * 252)


def max_drawdown(closes: Sequence[float]) -> float | None:
    """最大回撤（负数，如 -0.15 表示回撤 15%）。"""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (c - peak) / peak
        if dd < worst:
            worst = dd
    return worst


def ma_distance(closes: Sequence[float], window: int = 20) -> float | None:
    """收盘价相对于 MA(window) 的偏离度。"""
    if len(closes) < window:
        return None
    ma = sum(closes[-window:]) / window
    if ma <= 0:
        return None
    return closes[-1] / ma - 1.0


def relative_strength(
    asset_closes: Sequence[float],
    benchmark_closes: Sequence[float],
    window: int = 20,
) -> float | None:
    """相对基准的超额收益。"""
    asset_ret = period_return(asset_closes, window)
    bench_ret = period_return(benchmark_closes, window)
    if asset_ret is None or bench_ret is None:
        return None
    return asset_ret - bench_ret


def amount_ratio(
    amounts: Sequence[float],
    short_window: int = 5,
    long_window: int = 20,
) -> float | None:
    """短期成交额 / 长期成交额——反映近期资金关注度。"""
    if len(amounts) < long_window:
        return None
    short_avg = sum(amounts[-short_window:]) / short_window
    long_avg = sum(amounts[-long_window:]) / long_window
    if long_avg <= 0:
        return None
    return short_avg / long_avg


def build_score(
    factor_values: dict[str, float | None],
    weights: dict[str, float],
) -> float | None:
    """加权计算最终得分；任一因子为 None 时返回 None。"""
    score = 0.0
    for name, weight in weights.items():
        if name not in factor_values:
            return None
        val = factor_values[name]
        if val is None:
            return None
        score += val * weight
    return score
