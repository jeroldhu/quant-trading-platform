"""从锁定 qfq Gold 行情派生通用 ETF 日频特征。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from math import sqrt

from quant_trading.models import BarAdjustment, MarketBar


def _return(values: Sequence[float], periods: int) -> float | None:
    """计算严格 N 个交易期间收益，因此需要 N+1 个收盘价。"""
    if len(values) < periods + 1 or values[-(periods + 1)] <= 0:
        return None
    return values[-1] / values[-(periods + 1)] - 1.0


def _volatility(values: Sequence[float]) -> float | None:
    if len(values) < 21:
        return None
    window = values[-21:]
    returns = [window[index] / window[index - 1] - 1 for index in range(1, 21)]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return sqrt(variance * 252)


def _drawdown(values: Sequence[float]) -> float | None:
    if len(values) < 60:
        return None
    peak = values[-60]
    worst = 0.0
    for value in values[-60:]:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def build_etf_features(
    bars: Sequence[MarketBar],
    benchmark_bars: Sequence[MarketBar],
    *,
    data_version: str,
) -> list[dict[str, object]]:
    """生成契约字段；不足 61 根或零成交记录为不可评估。"""
    if any(bar.adjustment is not BarAdjustment.QFQ for bar in bars):
        raise ValueError("ETF 特征只能使用 qfq Gold")
    if any(bar.data_version != data_version for bar in bars):
        raise ValueError("ETF 特征输入包含其他 data_version")
    benchmark_close = {
        bar.trade_date: bar.close
        for bar in benchmark_bars
        if bar.trade_date and bar.data_version == data_version
    }
    benchmark_dates = sorted(benchmark_close)
    benchmark_history: dict[object, list[float]] = {}
    running_benchmark: list[float] = []
    for trade_date in benchmark_dates:
        running_benchmark.append(benchmark_close[trade_date])
        benchmark_history[trade_date] = list(running_benchmark)

    grouped: dict[str, list[MarketBar]] = defaultdict(list)
    for bar in bars:
        grouped[bar.instrument_id].append(bar)
    published_at = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for instrument, history in sorted(grouped.items()):
        closes: list[float] = []
        amounts: list[float] = []
        volumes: list[int] = []
        for bar in sorted(history, key=lambda item: item.trade_date):
            closes.append(bar.close)
            amounts.append(bar.amount)
            volumes.append(bar.volume)
            sufficient = len(closes) >= 61 and bar.volume > 0
            benchmark = benchmark_history.get(bar.trade_date, [])
            ret20 = _return(closes, 20) if sufficient else None
            ret60 = _return(closes, 60) if sufficient else None
            bench20 = _return(benchmark, 20) if sufficient else None
            bench60 = _return(benchmark, 60) if sufficient else None
            ma20 = sum(closes[-20:]) / 20 if sufficient else None
            ma60 = sum(closes[-60:]) / 60 if sufficient else None
            avg_amount20 = sum(amounts[-20:]) / 20 if sufficient else None
            avg_volume20 = sum(volumes[-20:]) / 20 if sufficient else None
            avg_amount5 = sum(amounts[-5:]) / 5 if sufficient else None
            rows.append(
                {
                    "instrument_id": instrument,
                    "trade_date": bar.trade_date,
                    "ret_1d": _return(closes, 1) if sufficient else None,
                    "ret_5d": _return(closes, 5) if sufficient else None,
                    "ret_20d": ret20,
                    "ret_60d": ret60,
                    "volatility_20d": _volatility(closes) if sufficient else None,
                    "drawdown_60d": _drawdown(closes) if sufficient else None,
                    "avg_amount_20d": avg_amount20,
                    "avg_volume_20d": avg_volume20,
                    "ma20": ma20,
                    "ma60": ma60,
                    "ma20_distance": (
                        closes[-1] / ma20 - 1.0 if ma20 is not None else None
                    ),
                    "relative_strength_20d": (
                        ret20 - bench20
                        if ret20 is not None and bench20 is not None
                        else None
                    ),
                    "relative_strength_60d": (
                        ret60 - bench60
                        if ret60 is not None and bench60 is not None
                        else None
                    ),
                    "amount_ratio_5_20": (
                        avg_amount5 / avg_amount20
                        if avg_amount5 is not None
                        and avg_amount20 is not None
                        and avg_amount20 > 0
                        else None
                    ),
                    "nav_premium": None,
                    "nav_premium_quality": "UNSUPPORTED",
                    "evaluation_status": "PASS" if sufficient else "UNEVALUABLE",
                    "data_version": data_version,
                    "published_at": published_at,
                    "schema_version": "1.0.0",
                }
            )
    return rows
