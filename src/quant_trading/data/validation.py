"""多源数据校验、共识选择与 Gold 融合。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from itertools import combinations
from statistics import median

from quant_trading.data.providers import SourceBar
from quant_trading.models import MarketBar, TradingStatus


class IssueSeverity(StrEnum):
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    issue_code: str
    detail: str
    severity: IssueSeverity
    instrument_id: str | None = None
    trade_date: date | None = None
    adjustment: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FusionResult:
    gold_bars: tuple[MarketBar, ...]
    issues: tuple[ValidationIssue, ...]
    quality_by_key: Mapping[tuple[str, date, str, str], tuple[str, str]]

    @property
    def passed(self) -> bool:
        return not any(
            issue.severity is IssueSeverity.BLOCKING for issue in self.issues
        )

    @property
    def blocking_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity is IssueSeverity.BLOCKING
        )


def count_independent_sources(bars: Iterable[SourceBar]) -> int:
    """相同上游域名的多个包装只计算一次。"""

    return len({bar.upstream_domain for bar in bars})


def _relative_difference(left: float, right: float) -> float:
    denominator = max(abs(left), abs(right))
    return abs(left - right) / denominator if denominator else 0.0


def _bars_agree(
    left: SourceBar, right: SourceBar, *, price_abs_tolerance: float
) -> bool:
    if left.upstream_domain == right.upstream_domain:
        return True
    if abs(left.close - right.close) > price_abs_tolerance:
        return False
    volume_mismatch = (
        _relative_difference(float(left.volume), float(right.volume)) > 0.001
        and abs(left.volume - right.volume) > 100
    )
    # 部分来源将手/份换算后可能留下小于一手的舍入差。
    return not volume_mismatch


def _largest_consensus(
    group: Sequence[SourceBar], *, price_abs_tolerance: float
) -> tuple[SourceBar, ...]:
    """选择最大的两两一致独立来源子集，避免固定优先级误隔离多数派。"""

    by_domain: dict[str, list[SourceBar]] = defaultdict(list)
    for bar in group:
        by_domain[bar.upstream_domain].append(bar)
    representatives = tuple(
        sorted(
            (
                sorted(items, key=lambda item: item.source_id)[0]
                for items in by_domain.values()
            ),
            key=lambda item: (item.upstream_domain, item.source_id),
        )
    )
    for size in range(len(representatives), 1, -1):
        candidates = [
            subset
            for subset in combinations(representatives, size)
            if all(
                _bars_agree(left, right, price_abs_tolerance=price_abs_tolerance)
                for left, right in combinations(subset, 2)
            )
        ]
        if candidates:
            return min(
                candidates,
                key=lambda subset: tuple(item.source_id for item in subset),
            )
    return ()


def _validate_range(bar: SourceBar) -> str | None:
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        return "价格必须全部为正"
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        return "low/high 未覆盖 open/close"
    if bar.volume < 0 or (bar.amount is not None and bar.amount < 0):
        return "成交量和成交额不能为负"
    return None


def fuse_gold_bars(
    bars: Sequence[SourceBar],
    *,
    data_version: str,
    min_independent_sources: int = 2,
    price_abs_tolerance: float = 0.001,
    published_at: datetime | None = None,
) -> FusionResult:
    """校验单源记录，隔离异常来源，并只发布达到共识的 Gold。"""

    if not bars:
        issue = ValidationIssue("EMPTY_INPUT", "无可校验行情", IssueSeverity.BLOCKING)
        return FusionResult((), (issue,), {})

    groups: dict[tuple[str, date, str], list[SourceBar]] = defaultdict(list)
    quality: dict[tuple[str, date, str, str], tuple[str, str]] = {}
    issues: list[ValidationIssue] = []
    for bar in bars:
        range_error = _validate_range(bar)
        key = (
            bar.instrument_id,
            bar.trade_date,
            bar.adjustment.value,
            bar.source_id,
        )
        if range_error:
            quality[key] = ("QUARANTINED", range_error)
            issues.append(
                ValidationIssue(
                    "PRICE_RANGE",
                    range_error,
                    IssueSeverity.BLOCKING,
                    bar.instrument_id,
                    bar.trade_date,
                    bar.adjustment.value,
                    (bar.source_id,),
                )
            )
            continue
        groups[(bar.instrument_id, bar.trade_date, bar.adjustment.value)].append(bar)

    gold: list[MarketBar] = []
    for group_key, group in sorted(groups.items()):
        instrument_id, trade_date, adjustment = group_key
        consensus = _largest_consensus(group, price_abs_tolerance=price_abs_tolerance)
        official_suspension = (
            len({item.upstream_domain for item in group}) == 1
            and all(item.upstream_domain == "tushare.pro" for item in group)
            and all(item.trading_status is TradingStatus.SUSPENDED for item in group)
        )
        if not consensus and official_suspension:
            consensus = (min(group, key=lambda item: item.source_id),)
        if len(consensus) < min_independent_sources and not official_suspension:
            source_ids = tuple(sorted(item.source_id for item in group))
            detail = (
                f"独立来源无 {min_independent_sources} 源共识: "
                f"{json.dumps(source_ids, ensure_ascii=False)}"
            )
            issues.append(
                ValidationIssue(
                    "SOURCE_MISMATCH",
                    detail,
                    IssueSeverity.BLOCKING,
                    instrument_id,
                    trade_date,
                    adjustment,
                    source_ids,
                )
            )
            for item in group:
                quality[(instrument_id, trade_date, adjustment, item.source_id)] = (
                    "QUARANTINED",
                    detail,
                )
            continue

        accepted = {item.source_id for item in consensus}
        for item in group:
            key = (instrument_id, trade_date, adjustment, item.source_id)
            quality[key] = (
                ("PASS", "已进入多源共识")
                if item.source_id in accepted
                else ("QUARANTINED", "不属于最大一致来源子集")
            )
            if item.source_id not in accepted:
                issues.append(
                    ValidationIssue(
                        "SOURCE_OUTLIER",
                        "不属于最大一致来源子集",
                        IssueSeverity.WARNING,
                        instrument_id,
                        trade_date,
                        adjustment,
                        (item.source_id,),
                    )
                )

        primary = min(consensus, key=lambda item: item.source_id)
        amounts = [item.amount for item in consensus if item.amount is not None]
        amount_flags: list[str] = []
        amount_source_count = len(amounts)
        if not amounts:
            amount = 0.0
            amount_flags.append("amount_missing")
        elif len(amounts) == 1:
            amount = amounts[0]
            amount_flags.append("amount_single_source")
        else:
            amount = float(median(amounts))
            if any(_relative_difference(value, amount) > 0.03 for value in amounts):
                amount_flags.append("amount_single_source")
                amount = amounts[0]
                issues.append(
                    ValidationIssue(
                        "AMOUNT_MISMATCH",
                        "成交额来源差异超过 3%，保留主来源并标记",
                        IssueSeverity.WARNING,
                        instrument_id,
                        trade_date,
                        adjustment,
                        tuple(sorted(accepted)),
                    )
                )

        source_map = {
            item.source_id: item.source_id in accepted
            for item in sorted(group, key=lambda item: item.source_id)
        }
        statuses = {item.trading_status for item in consensus}
        trading_status = (
            TradingStatus.SUSPENDED
            if TradingStatus.SUSPENDED in statuses
            else (
                TradingStatus.LIMIT_UP
                if TradingStatus.LIMIT_UP in statuses
                else (
                    TradingStatus.LIMIT_DOWN
                    if TradingStatus.LIMIT_DOWN in statuses
                    else TradingStatus.NORMAL
                )
            )
        )
        up_limits = [item.up_limit for item in consensus if item.up_limit is not None]
        down_limits = [
            item.down_limit for item in consensus if item.down_limit is not None
        ]
        if official_suspension:
            amount_flags.append("official_suspension_single_source")
        gold.append(
            MarketBar(
                instrument_id=instrument_id,
                trade_date=trade_date,
                adjustment=primary.adjustment,
                data_version=data_version,
                open=primary.open,
                high=primary.high,
                low=primary.low,
                close=primary.close,
                volume=primary.volume,
                amount=amount,
                pre_close=primary.pre_close,
                turnover_rate=primary.turnover_rate,
                trading_status=trading_status,
                up_limit=float(median(up_limits)) if up_limits else None,
                down_limit=float(median(down_limits)) if down_limits else None,
                is_st=any(item.is_st for item in consensus),
                source_count=count_independent_sources(consensus),
                source_map=source_map,
                quality_flags=tuple(amount_flags),
                amount_source_count=amount_source_count,
                adjustment_source=primary.source_id,
                adjustment_date=primary.adjustment_date,
                adjustment_version=primary.adjustment_version,
                published_at=published_at or datetime.now(UTC),
            )
        )

    return FusionResult(tuple(gold), tuple(issues), quality)


def validate_gold_bars(
    bars: Sequence[SourceBar], min_independent_sources: int = 2
) -> FusionResult:
    """兼容入口：调用方应优先使用 :func:`fuse_gold_bars`。"""

    return fuse_gold_bars(
        bars,
        data_version="validation-only",
        min_independent_sources=min_independent_sources,
    )
