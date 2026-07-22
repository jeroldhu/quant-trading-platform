"""交易所公告驱动的 A 股交易日历。

年度休市公告在新年度开始前发布，因此可以安全地为 T 日信号确定 T+1，且不会
借用未来行情。未配置年份会直接失败，禁止用“周一到周五”静默猜测。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from quant_trading.config import TradingCalendarConfig, load_trading_calendar_config


def calendar_config_hash(path: Path = Path("configs/trading_calendar.yaml")) -> str:
    """返回纳入 data_version 的公告配置哈希。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _calendar_bounds(start: date, end: date) -> tuple[date, date]:
    return date(start.year, 1, 1), date(end.year, 12, 31)


def _is_open(day: date, config: TradingCalendarConfig) -> tuple[bool, str | None]:
    try:
        year = config.years[day.year]
    except KeyError as exc:
        raise RuntimeError(
            f"交易日历未配置 {day.year} 年官方公告，拒绝猜测开市日"
        ) from exc
    if day.weekday() >= 5:
        return False, "weekend"
    for closure in year.closures:
        if closure.start <= day <= closure.end:
            return False, closure.reason
    return True, None


def is_official_trade_day(
    day: date, config: TradingCalendarConfig | None = None
) -> bool:
    """只依据已配置的交易所年度公告判断，缺失年份直接失败。"""
    return _is_open(day, config or load_trading_calendar_config())[0]


def latest_official_trade_day(
    on_or_before: date, config: TradingCalendarConfig | None = None
) -> date:
    """向前查找最近交易日，供 ``latest`` 参数解析使用。"""
    resolved = config or load_trading_calendar_config()
    current = on_or_before
    for _ in range(15):
        if _is_open(current, resolved)[0]:
            return current
        current -= timedelta(days=1)
    raise RuntimeError(f"{on_or_before} 前 15 日内没有已确认交易日")


def previous_official_trade_day(
    before: date,
    count: int,
    config: TradingCalendarConfig | None = None,
) -> date:
    """返回目标日前第 ``count`` 个交易日，用于增量重叠窗口。"""
    if count < 0:
        raise ValueError("count 不能为负")
    if count == 0:
        return before
    resolved = config or load_trading_calendar_config()
    current = before
    remaining = count
    for _ in range(max(366, count * 4)):
        current -= timedelta(days=1)
        if _is_open(current, resolved)[0]:
            remaining -= 1
            if remaining == 0:
                return current
    raise RuntimeError(f"无法在已配置日历中找到 {before} 前第 {count} 个交易日")


def build_official_trade_calendar(
    start: date,
    end: date,
    *,
    data_version: str,
    config: TradingCalendarConfig | None = None,
) -> list[dict[str, object]]:
    """构建双交易所一致确认的日历记录，包含可用时间和公告版本。"""
    if start > end:
        raise ValueError("calendar start 不能晚于 end")
    resolved = config or load_trading_calendar_config()
    full_start, full_end = _calendar_bounds(start, end)
    days: list[date] = []
    open_days: list[date] = []
    reasons: dict[date, str | None] = {}
    current = full_start
    while current <= full_end:
        is_open, reason = _is_open(current, resolved)
        days.append(current)
        reasons[current] = reason
        if is_open:
            open_days.append(current)
        current += timedelta(days=1)

    previous_by_day: dict[date, date | None] = {}
    previous: date | None = None
    open_set = set(open_days)
    for day in days:
        previous_by_day[day] = previous
        if day in open_set:
            previous = day
    next_by_day: dict[date, date | None] = {}
    following: date | None = None
    for day in reversed(days):
        next_by_day[day] = following
        if day in open_set:
            following = day

    published_at = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for day in days:
        if not start <= day <= end:
            continue
        year = resolved.years[day.year]
        sources = {source.source_id: True for source in year.sources}
        domains = tuple(source.upstream_domain for source in year.sources)
        source_versions = {source.source_id: source.version for source in year.sources}
        source_urls = {source.source_id: source.url for source in year.sources}
        available_at = max(source.available_at for source in year.sources)
        rows.append(
            {
                "calendar_date": day,
                "is_trade_day": day in open_set,
                "next_trade_day": next_by_day[day] if day in open_set else None,
                "prev_trade_day": previous_by_day[day] if day in open_set else None,
                "closure_reason": reasons[day],
                "source_count": len(set(domains)),
                "sources": json.dumps(sources, ensure_ascii=False),
                "source_versions": json.dumps(
                    source_versions, ensure_ascii=False, sort_keys=True
                ),
                "source_urls": json.dumps(
                    source_urls, ensure_ascii=False, sort_keys=True
                ),
                "available_at": available_at,
                "has_conflict": False,
                "data_version": data_version,
                "published_at": published_at,
                "schema_version": resolved.schema_version,
            }
        )
    return rows
