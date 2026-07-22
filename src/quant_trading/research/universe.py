"""Point-in-time 资产池解析。

不得用今天的成员回测历史；探索模式必须带幸存者偏差警告。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from quant_trading.data.storage import ParquetDuckDBStore
from quant_trading.models import MarketBar


class UniverseMode(StrEnum):
    POINT_IN_TIME = "point_in_time"
    FIXED_CURRENT = "fixed_current_universe"


@dataclass(frozen=True, slots=True)
class UniverseSelection:
    instruments: tuple[str, ...]
    as_of: date
    data_version: str
    mode: UniverseMode
    warnings: tuple[str, ...] = ()


class UniverseResolver:
    """从 Gold ETF master 解析目标日期成员，显式标记探索模式偏差。"""

    def __init__(self, store: ParquetDuckDBStore) -> None:
        self._store = store

    def resolve(
        self,
        as_of: date,
        *,
        data_version: str,
        master_data_version: str | None = None,
        mode: UniverseMode = UniverseMode.POINT_IN_TIME,
        current_as_of: date | None = None,
    ) -> UniverseSelection:
        effective_date = (
            current_as_of
            if mode is UniverseMode.FIXED_CURRENT and current_as_of is not None
            else as_of
        )
        instruments = self._store.get_universe(effective_date, master_data_version)
        if not instruments:
            raise RuntimeError(f"{effective_date} 没有可用的 point-in-time ETF master")
        warnings = (
            ("使用当前成员回放历史，存在幸存者偏差，不得作为正式结果",)
            if mode is UniverseMode.FIXED_CURRENT
            else ()
        )
        return UniverseSelection(
            instruments=instruments,
            as_of=as_of,
            data_version=data_version,
            mode=mode,
            warnings=warnings,
        )


def normalize_universe(instruments: Iterable[str]) -> tuple[str, ...]:
    """去重并稳定排序，保证相同输入产生确定结果。"""
    normalized = {instrument.strip().upper() for instrument in instruments}
    return tuple(sorted(i for i in normalized if i))


def filter_by_liquidity(
    bars: Sequence[MarketBar],
    as_of: date,
    min_avg_amount_20d: float = 50_000_000,
    min_active_days_20d: int = 18,
) -> tuple[str, ...]:
    """按 20 日均成交额和活跃天数过滤。"""
    from collections import defaultdict

    recent: dict[str, list[MarketBar]] = defaultdict(list)
    for bar in bars:
        if bar.trade_date <= as_of:
            recent[bar.instrument_id].append(bar)

    result: dict[str, list[MarketBar]] = {}
    for inst, history in recent.items():
        last20 = sorted(history, key=lambda b: b.trade_date)[-20:]
        if len(last20) < min_active_days_20d:
            continue
        avg_amt = sum(b.amount for b in last20) / len(last20)
        if avg_amt >= min_avg_amount_20d:
            result[inst] = last20

    return tuple(sorted(result))


def filter_by_history(
    instruments: Iterable[str],
    bars: Sequence[MarketBar],
    as_of: date,
    min_listed_days: int = 61,
) -> tuple[str, ...]:
    """过滤上市不足 min_listed_days 的标的。"""
    from collections import defaultdict

    eligible: list[str] = []
    instrument_set = set(instruments)

    day_counts: dict[str, int] = defaultdict(int)
    for bar in bars:
        if bar.instrument_id in instrument_set and bar.trade_date <= as_of:
            day_counts[bar.instrument_id] += 1

    for inst in instrument_set:
        if day_counts.get(inst, 0) >= min_listed_days:
            eligible.append(inst)
        else:
            pass  # 标记为"不可评估"

    return tuple(sorted(eligible))
