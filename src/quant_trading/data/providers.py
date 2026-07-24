"""行情数据源适配器。

Provider 只负责请求、失败分类和字段解析，不负责决定数据是否可以进入 Gold。
每次请求都返回原始响应，调用方必须先持久化并记录 SHA-256，再进入后续层级。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Protocol

import requests

from quant_trading.models import BarAdjustment, TradingStatus


class FetchState(StrEnum):
    """一次上游请求的明确终态。"""

    SUCCESS = "SUCCESS"
    NO_DATA = "NO_DATA"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    THROTTLED = "THROTTLED"
    QUARANTINED = "QUARANTINED"


class ProviderFailure(RuntimeError):
    """携带可审计失败类别的数据源异常。"""

    def __init__(self, state: FetchState, source_id: str, detail: str) -> None:
        super().__init__(f"{source_id} [{state.value}]: {detail}")
        self.state = state
        self.source_id = source_id
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SourceBar:
    """Bronze/Silver 使用的单来源日线记录。"""

    instrument_id: str
    trade_date: date
    adjustment: BarAdjustment
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float | None
    pre_close: float | None
    turnover_rate: float | None
    source_id: str
    upstream_domain: str
    available_at: datetime
    raw_hash: str
    adjustment_date: date
    adjustment_version: str
    trading_status: TradingStatus = TradingStatus.NORMAL
    up_limit: float | None = None
    down_limit: float | None = None
    is_st: bool = False


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    """ETF master 单来源记录，用于构建 point-in-time SCD。"""

    instrument_id: str
    code: str
    exchange: str
    name: str
    listing_date: date | None
    effective_date: date
    source_id: str
    upstream_domain: str
    available_at: datetime


@dataclass(frozen=True, slots=True)
class FetchResult:
    """行情请求结果；原始响应与解析记录必须同时保留。"""

    source_id: str
    upstream_domain: str
    dataset: str
    fetched_at: datetime
    request_params: Mapping[str, object]
    status_code: int
    content_type: str
    content: bytes
    state: FetchState
    error_detail: str | None = None
    bars: tuple[SourceBar, ...] = ()
    instruments: tuple[InstrumentRecord, ...] = ()

    @property
    def raw_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _parse_bars_safely(
    parser: Callable[[], tuple[SourceBar, ...]],
) -> tuple[tuple[SourceBar, ...], FetchState, str | None]:
    """解析失败返回 QUARANTINED，让调用方仍能先保存 Raw 响应。"""
    try:
        bars = parser()
    except ProviderFailure as exc:
        return (), exc.state, " ".join(exc.detail.split())[:500]
    except (KeyError, TypeError, ValueError) as exc:
        return (), FetchState.QUARANTINED, " ".join(str(exc).split())[:500]
    return bars, FetchState.SUCCESS if bars else FetchState.NO_DATA, None


def _within_date_range(
    bars: tuple[SourceBar, ...], start: date, end: date
) -> tuple[SourceBar, ...]:
    """在统一完成解析后裁剪日期，避免 Provider 各自形成隐式边界。"""
    return tuple(bar for bar in bars if start <= bar.trade_date <= end)


def _valid_tdx_rows(rows: Sequence[Mapping[str, object]]) -> bool:
    """拒绝能建立 TCP 连接但返回损坏日期或价格的通达信节点。"""
    if not rows:
        return False
    try:
        for row in rows:
            day = datetime.fromisoformat(str(row["datetime"])).date()
            if not date(1990, 1, 1) <= day <= date.today() + timedelta(days=7):
                return False
            if (
                min(float(str(row[key])) for key in ("open", "high", "low", "close"))
                <= 0
            ):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


class BarProvider(Protocol):
    """所有日线 Provider 的最小协议。"""

    source_id: str
    upstream_domain: str
    supports_adjustments: tuple[BarAdjustment, ...]

    def fetch_daily_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        adjustment: BarAdjustment,
    ) -> Sequence[FetchResult]: ...


class MasterProvider(Protocol):
    source_id: str
    upstream_domain: str

    def fetch_master(self, as_of: date) -> Sequence[FetchResult]: ...


def _instrument_parts(instrument_id: str) -> tuple[str, str]:
    """同时接受 ``510300.SH`` 与 ``SH.510300``，统一返回 SH/SZ + code。"""

    value = instrument_id.strip().upper()
    if "." not in value:
        raise ValueError(f"证券代码缺少交易所: {instrument_id}")
    left, right = value.split(".", 1)
    if left in {"SH", "SZ"}:
        exchange, code = left, right
    else:
        code, exchange = left, right
    if exchange not in {"SH", "SZ"} or len(code) != 6 or not code.isdigit():
        raise ValueError(f"非法证券代码: {instrument_id}")
    return exchange, code


def normalize_instrument(instrument_id: str) -> str:
    exchange, code = _instrument_parts(instrument_id)
    return f"{code}.{exchange}"


def _float(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


class HttpProvider:
    """带速率限制、分类重试和单任务熔断的 HTTP 基类。"""

    source_id = "http"
    upstream_domain = ""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        requests_per_second: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._interval = 1.0 / requests_per_second
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 AppleWebKit/537.36 Chrome/125 QuantTradingPlatform/0.1"
                )
            }
        )
        self._last_request_at = 0.0
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None

    def _wait_rate_limit(self) -> None:
        delay = self._interval - (time.monotonic() - self._last_request_at)
        if delay > 0:
            time.sleep(delay)

    def _get(
        self,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
    ) -> requests.Response:
        if self._circuit_opened_at is not None:
            elapsed = time.monotonic() - self._circuit_opened_at
            if elapsed < 30 * 60:
                raise ProviderFailure(
                    FetchState.FAILED,
                    self.source_id,
                    "连续失败达到阈值，熔断器仍在冷却",
                )
            self._consecutive_failures = 0
            self._circuit_opened_at = None

        last_error = ""
        for attempt, delay in enumerate((0.0, 1.0, 2.0), start=1):
            if delay:
                time.sleep(delay)
            self._wait_rate_limit()
            self._last_request_at = time.monotonic()
            try:
                response = self._session.get(
                    url,
                    params=dict(params),
                    headers=dict(headers) if headers else None,
                    timeout=self._timeout,
                )
                if response.status_code == 429:
                    raise ProviderFailure(
                        FetchState.THROTTLED, self.source_id, "HTTP 429"
                    )
                if 400 <= response.status_code < 500:
                    raise ProviderFailure(
                        FetchState.UNAVAILABLE,
                        self.source_id,
                        f"HTTP {response.status_code}",
                    )
                response.raise_for_status()
                self._consecutive_failures = 0
                return response
            except ProviderFailure:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = f"第 {attempt} 次请求失败: {exc}"

        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._circuit_opened_at = time.monotonic()
        raise ProviderFailure(FetchState.FAILED, self.source_id, last_error)


class EastmoneyProvider(HttpProvider):
    """东方财富 ETF 全市场快照与 raw/qfq 历史日线。"""

    source_id: str = "eastmoney"
    upstream_domain: str = "eastmoney.com"
    supports_adjustments: tuple[BarAdjustment, ...] = (
        BarAdjustment.RAW,
        BarAdjustment.QFQ,
    )
    snapshot_url = "https://push2delay.eastmoney.com/api/qt/clist/get"
    history_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    snapshot_fields = "f2,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f26,f297"

    def fetch_master(self, as_of: date) -> Sequence[FetchResult]:
        results: list[FetchResult] = []
        seen: set[str] = set()
        page = 1
        total = 1
        # total 包含 LOF/封闭基金等非 ETF 品种，不可靠。
        # 循环由内部 break 终止——当 parse_master 连续过滤为空时退出。
        while True:
            params = {
                "pn": str(page),
                "pz": "100",
                "po": "1",
                "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2",
                "invt": "2",
                "fid": "f12",
                "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
                "fields": self.snapshot_fields,
            }
            response = self._get(self.snapshot_url, params)
            fetched_at = datetime.now(UTC)
            try:
                payload = json.loads(response.content)
                data = payload.get("data") or {}
                total = int(data.get("total") or 0)
                rows = data.get("diff") or []
                records = self.parse_master(rows, fetched_at, as_of)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                results.append(
                    FetchResult(
                        source_id="eastmoney_etf_snapshot",
                        upstream_domain=self.upstream_domain,
                        dataset="etf_master",
                        fetched_at=fetched_at,
                        request_params={"url": self.snapshot_url, **params},
                        status_code=response.status_code,
                        content_type=response.headers.get(
                            "content-type", "application/json"
                        ),
                        content=response.content,
                        state=FetchState.QUARANTINED,
                        error_detail=" ".join(str(exc).split())[:500],
                    )
                )
                break
            if page == 1 and (total <= 0 or not records):
                raise ProviderFailure(
                    FetchState.NO_DATA, self.source_id, "ETF 全市场快照为空"
                )
            new_ids = {item.instrument_id for item in records} - seen
            if records and not new_ids:
                raise ProviderFailure(
                    FetchState.QUARANTINED,
                    self.source_id,
                    f"全市场分页在第 {page} 页没有推进",
                )
            # parse_master 只保留真实 ETF；API 返回的 total 包含 LOF/封闭基金等，
            # 当后续页全部被 parse_master 过滤为空时，提前终止分页。
            if not records:
                break
            seen.update(new_ids)
            results.append(
                FetchResult(
                    source_id="eastmoney_etf_snapshot",
                    upstream_domain=self.upstream_domain,
                    dataset="etf_master",
                    fetched_at=fetched_at,
                    request_params={"url": self.snapshot_url, **params},
                    status_code=response.status_code,
                    content_type=response.headers.get(
                        "content-type", "application/json"
                    ),
                    content=response.content,
                    state=FetchState.SUCCESS,
                    instruments=records,
                )
            )
            page += 1
            if page > total // 100 + 3:
                raise ProviderFailure(
                    FetchState.QUARANTINED, self.source_id, "ETF 全市场分页未正常结束"
                )
        return results

    @staticmethod
    def parse_master(
        rows: Sequence[Mapping[str, object]],
        fetched_at: datetime,
        as_of: date,
    ) -> tuple[InstrumentRecord, ...]:
        records: list[InstrumentRecord] = []
        for row in rows:
            code = str(row.get("f12") or "").zfill(6)
            market = str(row.get("f13") or "")
            if not code.isdigit() or market not in {"0", "1"}:
                continue
            exchange = "SH" if market == "1" else "SZ"
            listing_raw = str(row.get("f26") or "")
            try:
                listing_date = datetime.strptime(listing_raw, "%Y%m%d").date()
            except ValueError:
                listing_date = None
            records.append(
                InstrumentRecord(
                    instrument_id=f"{code}.{exchange}",
                    code=code,
                    exchange=exchange,
                    name=str(row.get("f14") or ""),
                    listing_date=listing_date,
                    effective_date=as_of,
                    source_id="eastmoney_etf_snapshot",
                    upstream_domain="eastmoney.com",
                    available_at=fetched_at,
                )
            )
        return tuple(records)

    def fetch_daily_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        adjustment: BarAdjustment,
    ) -> Sequence[FetchResult]:
        results: list[FetchResult] = []
        for instrument in instruments:
            normalized = normalize_instrument(instrument)
            exchange, code = _instrument_parts(normalized)
            params = {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "klt": "101",
                "fqt": "0" if adjustment is BarAdjustment.RAW else "1",
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "lmt": "1000000",
                "secid": f"{'1' if exchange == 'SH' else '0'}.{code}",
            }
            response = self._get(self.history_url, params)
            fetched_at = datetime.now(UTC)
            raw_hash = hashlib.sha256(response.content).hexdigest()
            bars, state, error_detail = _parse_bars_safely(
                partial(
                    self.parse_history,
                    response.content,
                    normalized,
                    fetched_at,
                    adjustment,
                    raw_hash,
                )
            )
            results.append(
                FetchResult(
                    source_id=f"eastmoney_kline_{adjustment.value}",
                    upstream_domain=self.upstream_domain,
                    dataset="etf_daily_bar",
                    fetched_at=fetched_at,
                    request_params={"url": self.history_url, **params},
                    status_code=response.status_code,
                    content_type=response.headers.get(
                        "content-type", "application/json"
                    ),
                    content=response.content,
                    state=state,
                    error_detail=error_detail,
                    bars=bars,
                )
            )
        return results

    @staticmethod
    def parse_history(
        content: bytes,
        instrument_id: str,
        fetched_at: datetime,
        adjustment: BarAdjustment,
        raw_hash: str,
    ) -> tuple[SourceBar, ...]:
        payload = json.loads(content)
        rows = ((payload.get("data") or {}).get("klines")) or []
        parsed: list[SourceBar] = []
        previous: float | None = None
        for value in rows:
            parts = str(value).split(",")
            if len(parts) < 11:
                continue
            open_, close, high, low = map(float, parts[1:5])
            parsed.append(
                SourceBar(
                    instrument_id=instrument_id,
                    trade_date=date.fromisoformat(parts[0]),
                    adjustment=adjustment,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=round(float(parts[5]) * 100),
                    amount=_float(parts[6]),
                    pre_close=previous,
                    turnover_rate=_float(parts[10]),
                    source_id=f"eastmoney_kline_{adjustment.value}",
                    upstream_domain="eastmoney.com",
                    available_at=fetched_at,
                    raw_hash=raw_hash,
                    adjustment_date=fetched_at.date(),
                    adjustment_version=raw_hash[:16],
                )
            )
            previous = close
        return tuple(parsed)


class TencentProvider(HttpProvider):
    """腾讯 qfq 历史日线校验源；接口按最多 640 根向前分页。"""

    source_id: str = "tencent"
    upstream_domain: str = "gtimg.cn"
    supports_adjustments: tuple[BarAdjustment, ...] = (BarAdjustment.QFQ,)
    history_url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

    def fetch_daily_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        adjustment: BarAdjustment,
    ) -> Sequence[FetchResult]:
        if adjustment is not BarAdjustment.QFQ:
            raise ProviderFailure(
                FetchState.UNAVAILABLE,
                self.source_id,
                "腾讯适配器只发布 qfq；空复权参数会被上游拒绝",
            )
        results: list[FetchResult] = []
        for instrument in instruments:
            normalized = normalize_instrument(instrument)
            exchange, code = _instrument_parts(normalized)
            symbol = f"{exchange.lower()}{code}"
            cursor_end = end
            for page in range(1, 30):
                params = {
                    "param": (
                        f"{symbol},day,{start.isoformat()},"
                        f"{cursor_end.isoformat()},640,qfq"
                    )
                }
                response = self._get(self.history_url, params)
                fetched_at = datetime.now(UTC)
                raw_hash = hashlib.sha256(response.content).hexdigest()
                bars, state, error_detail = _parse_bars_safely(
                    partial(
                        self.parse_history,
                        response.content,
                        normalized,
                        fetched_at,
                        adjustment,
                        raw_hash,
                    )
                )
                bars = _within_date_range(bars, start, cursor_end)
                if state is FetchState.SUCCESS and not bars:
                    state = FetchState.NO_DATA
                results.append(
                    FetchResult(
                        source_id=f"tencent_kline_{adjustment.value}",
                        upstream_domain=self.upstream_domain,
                        dataset="etf_daily_bar",
                        fetched_at=fetched_at,
                        request_params={
                            "url": self.history_url,
                            "page": page,
                            **params,
                        },
                        status_code=response.status_code,
                        content_type=response.headers.get(
                            "content-type", "application/json"
                        ),
                        content=response.content,
                        state=state,
                        error_detail=error_detail,
                        bars=bars,
                    )
                )
                if state is not FetchState.SUCCESS:
                    break
                earliest = min(bar.trade_date for bar in bars)
                if earliest <= start:
                    break
                cursor_end = earliest - timedelta(days=1)
            else:
                raise ProviderFailure(
                    FetchState.QUARANTINED,
                    self.source_id,
                    f"{normalized} 分页超过 29 页",
                )
        return results

    @staticmethod
    def parse_history(
        content: bytes,
        instrument_id: str,
        fetched_at: datetime,
        adjustment: BarAdjustment,
        raw_hash: str,
    ) -> tuple[SourceBar, ...]:
        payload = json.loads(content)
        exchange, code = _instrument_parts(instrument_id)
        symbol = f"{exchange.lower()}{code}"
        stock_data = (payload.get("data") or {}).get(symbol) or {}
        key = "qfqday" if adjustment is BarAdjustment.QFQ else "day"
        rows = (
            stock_data.get(key)
            or (stock_data.get("day") if key == "qfqday" else [])
            or []
        )
        parsed: list[SourceBar] = []
        previous: float | None = None
        version = str(stock_data.get("version") or raw_hash[:16])
        for value in rows:
            if len(value) < 6:
                continue
            open_, close, high, low = map(float, value[1:5])
            parsed.append(
                SourceBar(
                    instrument_id=instrument_id,
                    trade_date=date.fromisoformat(str(value[0])),
                    adjustment=adjustment,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=round(float(value[5]) * 100),
                    amount=None,
                    pre_close=previous,
                    turnover_rate=None,
                    source_id=f"tencent_kline_{adjustment.value}",
                    upstream_domain="gtimg.cn",
                    available_at=fetched_at,
                    raw_hash=raw_hash,
                    adjustment_date=fetched_at.date(),
                    adjustment_version=version,
                )
            )
            previous = close
        return tuple(parsed)


class ThsProvider(HttpProvider):
    """同花顺 raw/qfq 历史日线与成交额校验源。"""

    source_id: str = "ths"
    upstream_domain: str = "10jqka.com.cn"
    supports_adjustments: tuple[BarAdjustment, ...] = (
        BarAdjustment.RAW,
        BarAdjustment.QFQ,
    )
    endpoint_template = (
        "https://d.10jqka.com.cn/v6/line/{market}_{code}/{mode}/last36000.js"
    )

    def fetch_daily_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        adjustment: BarAdjustment,
    ) -> Sequence[FetchResult]:
        results: list[FetchResult] = []
        for instrument in instruments:
            normalized = normalize_instrument(instrument)
            exchange, code = _instrument_parts(normalized)
            mode = "00" if adjustment is BarAdjustment.RAW else "01"
            url = self.endpoint_template.format(
                market=exchange.lower(), code=code, mode=mode
            )
            response = self._get(
                url, {}, headers={"Referer": "https://q.10jqka.com.cn/"}
            )
            fetched_at = datetime.now(UTC)
            raw_hash = hashlib.sha256(response.content).hexdigest()
            bars, state, error_detail = _parse_bars_safely(
                partial(
                    self.parse_history,
                    response.content,
                    normalized,
                    fetched_at,
                    adjustment,
                    raw_hash,
                )
            )
            bars = _within_date_range(bars, start, end)
            if state is FetchState.SUCCESS and not bars:
                state = FetchState.NO_DATA
            results.append(
                FetchResult(
                    source_id=f"ths_kline_{adjustment.value}",
                    upstream_domain=self.upstream_domain,
                    dataset="etf_daily_bar",
                    fetched_at=fetched_at,
                    request_params={
                        "url": url,
                        "mode": mode,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    },
                    status_code=response.status_code,
                    content_type=response.headers.get(
                        "content-type", "application/javascript"
                    ),
                    content=response.content,
                    state=state,
                    error_detail=error_detail,
                    bars=bars,
                )
            )
        return results

    @staticmethod
    def parse_history(
        content: bytes,
        instrument_id: str,
        fetched_at: datetime,
        adjustment: BarAdjustment,
        raw_hash: str,
    ) -> tuple[SourceBar, ...]:
        text = content.decode("utf-8", errors="replace")
        if "Nginx forbidden" in text:
            raise ProviderFailure(FetchState.THROTTLED, "ths", "Nginx forbidden")
        first, last = text.find("{"), text.rfind("}")
        if first < 0 or last < first:
            raise ProviderFailure(
                FetchState.QUARANTINED, "ths", "响应不是合法 line payload"
            )
        payload = json.loads(text[first : last + 1])
        rows = [
            row.split(",")
            for row in str(payload.get("data") or "").split(";")
            if len(row.split(",")) >= 7
        ]
        parsed: list[SourceBar] = []
        previous: float | None = None
        for row in rows:
            volume = _float(row[5])
            if volume is None or volume <= 0:
                # 即使跳过该 bar，仍需更新 pre_close 链
                # 避免下一个有效 bar 的 pre_close 指向过远的日期
                if len(row) > 4:
                    previous = _float(row[4])
                continue
            open_, high, low, close = map(float, row[1:5])
            parsed.append(
                SourceBar(
                    instrument_id=instrument_id,
                    trade_date=datetime.strptime(row[0], "%Y%m%d").date(),
                    adjustment=adjustment,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=round(volume),
                    amount=_float(row[6]),
                    pre_close=previous,
                    turnover_rate=None,
                    source_id=f"ths_kline_{adjustment.value}",
                    upstream_domain="10jqka.com.cn",
                    available_at=fetched_at,
                    raw_hash=raw_hash,
                    adjustment_date=fetched_at.date(),
                    adjustment_version=raw_hash[:16],
                )
            )
            previous = close
        return tuple(parsed)


class TdxProvider:
    """通达信日线及除权因子校验源；需要安装 ``market`` extra。"""

    source_id: str = "tdx"
    upstream_domain: str = "tdx.com.cn"
    # pytdx 返回未复权 K 线。除权除息事件包含分红、配股、送转等多种语义，
    # 在没有逐事件复权校验器前不得把“只处理缩股”的近似结果冒充正式 qfq。
    supports_adjustments: tuple[BarAdjustment, ...] = (BarAdjustment.RAW,)
    hosts = (
        ("180.153.18.170", 7709),
        ("202.108.253.130", 7709),
        ("14.17.75.71", 7709),
        ("119.147.212.81", 7709),
    )

    def fetch_master(self, as_of: date) -> Sequence[FetchResult]:
        """读取沪深证券清单，作为东方财富 ETF master 的独立成员校验源。"""
        try:
            api_class = importlib.import_module("pytdx.hq").__dict__["TdxHq_API"]
        except (ImportError, KeyError) as exc:
            raise ProviderFailure(
                FetchState.UNAVAILABLE,
                self.source_id,
                "缺少 pytdx；请安装 market extra",
            ) from exc
        api = api_class(heartbeat=False, auto_retry=False, raise_exception=False)
        active_host: tuple[str, int] | None = None
        for host, port in self.hosts:
            if api.connect(host, port, time_out=3):
                active_host = (host, port)
                break
        if active_host is None:
            raise ProviderFailure(
                FetchState.FAILED, self.source_id, "没有可连接的通达信节点"
            )
        raw_rows: list[dict[str, object]] = []
        try:
            for market_id in (0, 1):
                total = int(api.get_security_count(market_id) or 0)
                for offset in range(0, total, 1000):
                    batch = api.get_security_list(market_id, offset) or []
                    raw_rows.extend(
                        {**dict(item), "market_id": market_id} for item in batch
                    )
        finally:
            api.disconnect()
        content = json.dumps(
            {"host": active_host, "rows": raw_rows, "as_of": as_of.isoformat()},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode()
        fetched_at = datetime.now(UTC)
        records = self.parse_master(raw_rows, fetched_at, as_of)
        if not records:
            raise ProviderFailure(
                FetchState.NO_DATA, self.source_id, "通达信 ETF 证券清单为空"
            )
        return (
            FetchResult(
                source_id="tdx_etf_master",
                upstream_domain=self.upstream_domain,
                dataset="etf_master",
                fetched_at=fetched_at,
                request_params={
                    "endpoint": "tdx://get_security_list",
                    "host": str(active_host),
                },
                status_code=200,
                content_type="application/json",
                content=content,
                state=FetchState.SUCCESS,
                instruments=records,
            ),
        )

    @staticmethod
    def parse_master(
        rows: Sequence[Mapping[str, object]],
        fetched_at: datetime,
        as_of: date,
    ) -> tuple[InstrumentRecord, ...]:
        """用证券名称与交易所代码段识别 ETF，不把普通 LOF 混入基础范围。"""
        records: list[InstrumentRecord] = []
        for row in rows:
            code = str(row.get("code") or "")
            name = str(row.get("name") or "").strip()
            market_id = int(str(row.get("market_id") or 0))
            exchange = "SH" if market_id == 1 else "SZ"
            code_family = (
                code.startswith(("51", "56", "58"))
                if exchange == "SH"
                else code.startswith("159")
            )
            if len(code) != 6 or not code.isdigit() or not code_family:
                continue
            records.append(
                InstrumentRecord(
                    instrument_id=f"{code}.{exchange}",
                    code=code,
                    exchange=exchange,
                    name=name,
                    listing_date=None,
                    effective_date=as_of,
                    source_id="tdx_etf_master",
                    upstream_domain="tdx.com.cn",
                    available_at=fetched_at,
                )
            )
        return tuple(records)

    def fetch_daily_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        adjustment: BarAdjustment,
    ) -> Sequence[FetchResult]:
        if adjustment is not BarAdjustment.RAW:
            raise ProviderFailure(
                FetchState.UNAVAILABLE,
                self.source_id,
                "通达信适配器当前只发布 raw；qfq 使用 HTTP 三源共识",
            )
        try:
            api_class = importlib.import_module("pytdx.hq").__dict__["TdxHq_API"]
        except (ImportError, KeyError) as exc:
            raise ProviderFailure(
                FetchState.UNAVAILABLE,
                self.source_id,
                "缺少 pytdx；请安装 market extra",
            ) from exc
        results: list[FetchResult] = []
        for instrument in instruments:
            normalized = normalize_instrument(instrument)
            exchange, code = _instrument_parts(normalized)
            market_id = 1 if exchange == "SH" else 0
            api = api_class(heartbeat=False, auto_retry=False, raise_exception=False)
            active_host: tuple[str, int] | None = None
            for host, port in self.hosts:
                if not api.connect(host, port, time_out=3):
                    continue
                probe = api.get_security_bars(9, market_id, code, 0, 10) or []
                if _valid_tdx_rows([dict(item) for item in probe]):
                    active_host = (host, port)
                    break
                api.disconnect()
            if active_host is None:
                raise ProviderFailure(
                    FetchState.FAILED, self.source_id, "没有可连接的通达信节点"
                )
            rows: list[dict[str, object]] = []
            try:
                offset = 0
                while offset < 20_000:
                    batch = api.get_security_bars(9, market_id, code, offset, 800) or []
                    if not batch:
                        break
                    serialized = [dict(item) for item in batch]
                    rows.extend(serialized)
                    valid_dates: list[date] = []
                    for item in serialized:
                        try:
                            valid_dates.append(
                                datetime.fromisoformat(str(item["datetime"])).date()
                            )
                        except (KeyError, ValueError):
                            # 原始异常行继续进入 content，由统一安全解析器隔离并留档。
                            continue
                    if not valid_dates:
                        break
                    earliest = min(valid_dates)
                    if earliest <= start:
                        break
                    offset += len(serialized)
            finally:
                api.disconnect()
            content = json.dumps(
                {
                    "host": active_host,
                    "rows": rows,
                    "requested_end": end.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode()
            fetched_at = datetime.now(UTC)
            raw_hash = hashlib.sha256(content).hexdigest()
            bars, state, error_detail = _parse_bars_safely(
                partial(
                    self.parse_history,
                    content,
                    normalized,
                    fetched_at,
                    adjustment,
                    raw_hash,
                )
            )
            bars = _within_date_range(bars, start, end)
            if state is FetchState.SUCCESS and not bars:
                state = FetchState.NO_DATA
            results.append(
                FetchResult(
                    source_id=f"tdx_kline_{adjustment.value}",
                    upstream_domain=self.upstream_domain,
                    dataset="etf_daily_bar",
                    fetched_at=fetched_at,
                    request_params={
                        "endpoint": "tdx://get_security_bars",
                        "host": str(active_host),
                        "code": code,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    },
                    status_code=200,
                    content_type="application/json",
                    content=content,
                    state=state,
                    error_detail=error_detail,
                    bars=bars,
                )
            )
        return results

    @staticmethod
    def parse_history(
        content: bytes,
        instrument_id: str,
        fetched_at: datetime,
        adjustment: BarAdjustment,
        raw_hash: str,
    ) -> tuple[SourceBar, ...]:
        payload = json.loads(content)
        requested_end = date.fromisoformat(str(payload["requested_end"]))
        if adjustment is not BarAdjustment.RAW:
            raise ValueError("通达信解析器禁止生成未经完整事件校验的 qfq")
        rows = sorted(
            payload.get("rows", []), key=lambda item: str(item.get("datetime", ""))
        )
        parsed: list[SourceBar] = []
        previous: float | None = None
        for row in rows:
            trade_date = datetime.fromisoformat(str(row["datetime"])).date()
            prices = {key: float(row[key]) for key in ("open", "high", "low", "close")}
            volume = _float(row.get("vol"))
            if volume is None:
                volume = _float(row.get("volume"))
            if volume is None:
                volume = 0.0
            parsed.append(
                SourceBar(
                    instrument_id=instrument_id,
                    trade_date=trade_date,
                    adjustment=adjustment,
                    open=prices["open"],
                    high=prices["high"],
                    low=prices["low"],
                    close=prices["close"],
                    volume=round(volume * 100),
                    amount=_float(row.get("amount")),
                    pre_close=previous,
                    turnover_rate=None,
                    source_id=f"tdx_kline_{adjustment.value}",
                    upstream_domain="tdx.com.cn",
                    available_at=fetched_at,
                    raw_hash=raw_hash,
                    adjustment_date=requested_end,
                    adjustment_version="raw-no-adjustment",
                )
            )
            previous = prices["close"]
        return tuple(parsed)


class TushareStockProvider(HttpProvider):
    """Tushare 股票日线及交易约束源；Token 只由调用方从环境变量注入。"""

    source_id = "tushare"
    upstream_domain = "tushare.pro"
    supports_adjustments: tuple[BarAdjustment, ...] = (
        BarAdjustment.RAW,
        BarAdjustment.QFQ,
    )
    api_url = "https://api.tushare.pro"

    def __init__(self, token: str, **kwargs: object) -> None:
        if not token:
            raise ValueError("Tushare token 不能为空")
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._token = token

    def _query(
        self,
        api_name: str,
        params: Mapping[str, str],
        fields: str,
    ) -> FetchResult:
        """执行一次官方 POST API，并保留包括权限错误在内的原始响应。"""
        self._wait_rate_limit()
        fetched_at = datetime.now(UTC)
        try:
            response = self._session.post(
                self.api_url,
                json={
                    "api_name": api_name,
                    "token": self._token,
                    "params": dict(params),
                    "fields": fields,
                },
                timeout=self._timeout,
            )
            self._last_request_at = time.monotonic()
        except requests.RequestException as exc:
            raise ProviderFailure(FetchState.FAILED, self.source_id, str(exc)) from exc
        state = FetchState.SUCCESS
        error_detail: str | None = None
        try:
            payload = response.json()
            code = int(payload.get("code", -1))
            if code != 0:
                message = str(payload.get("msg") or f"Tushare code={code}")
                state = (
                    FetchState.THROTTLED
                    if "频" in message or "limit" in message.lower()
                    else FetchState.UNAVAILABLE
                )
                error_detail = message[:500]
        except (TypeError, ValueError, requests.JSONDecodeError) as exc:
            state = FetchState.QUARANTINED
            error_detail = " ".join(str(exc).split())[:500]
        return FetchResult(
            source_id=f"tushare_{api_name}",
            upstream_domain=self.upstream_domain,
            dataset=api_name,
            fetched_at=fetched_at,
            request_params={"url": self.api_url, "api_name": api_name, **params},
            status_code=response.status_code,
            content_type=response.headers.get("content-type", "application/json"),
            content=response.content,
            state=state,
            error_detail=error_detail,
        )

    @staticmethod
    def _rows(result: FetchResult) -> list[dict[str, object]]:
        payload = json.loads(result.content)
        data = payload.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        if not isinstance(fields, list) or not isinstance(items, list):
            raise ValueError(f"{result.source_id} 返回非法 data.fields/items")
        return [
            dict(zip((str(field) for field in fields), item, strict=True))
            for item in items
        ]

    def fetch_stock_bundle(
        self, instrument: str, start: date, end: date
    ) -> tuple[tuple[FetchResult, ...], FetchResult]:
        """拉取一只股票的 raw/qfq 和涨跌停、停牌、ST 约束。"""
        normalized = normalize_instrument(instrument)
        params = {
            "ts_code": normalized,
            "start_date": start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
        }
        raw_results = (
            self._query(
                "daily",
                params,
                "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
            ),
            self._query("adj_factor", params, "ts_code,trade_date,adj_factor"),
            self._query(
                "stk_limit",
                params,
                "ts_code,trade_date,pre_close,up_limit,down_limit",
            ),
            self._query(
                "suspend_d",
                {**params, "suspend_type": "S"},
                "ts_code,trade_date,suspend_type,suspend_timing",
            ),
            self._query("stock_st", params, "ts_code,name,trade_date,type,type_name"),
        )
        failed = [
            result for result in raw_results if result.state is not FetchState.SUCCESS
        ]
        content = json.dumps(
            {
                "instrument": normalized,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "raw_hashes": [result.raw_hash for result in raw_results],
            },
            sort_keys=True,
        ).encode()
        fetched_at = max(result.fetched_at for result in raw_results)
        if failed:
            detail = "; ".join(
                f"{item.source_id}:{item.state.value}({item.error_detail or ''})"
                for item in failed
            )
            return raw_results, FetchResult(
                source_id="tushare_stock_bundle",
                upstream_domain=self.upstream_domain,
                dataset="stock_daily_bar",
                fetched_at=fetched_at,
                request_params=params,
                status_code=200,
                content_type="application/json",
                content=content,
                state=FetchState.UNAVAILABLE,
                error_detail=detail[:500],
            )
        try:
            bars = self._build_stock_bars(raw_results, normalized, end, fetched_at)
            state = FetchState.SUCCESS if bars else FetchState.NO_DATA
            error_detail = None
        except (KeyError, TypeError, ValueError) as exc:
            bars = ()
            state = FetchState.QUARANTINED
            error_detail = " ".join(str(exc).split())[:500]
        return raw_results, FetchResult(
            source_id="tushare_stock_bundle",
            upstream_domain=self.upstream_domain,
            dataset="stock_daily_bar",
            fetched_at=fetched_at,
            request_params=params,
            status_code=200,
            content_type="application/json",
            content=content,
            state=state,
            error_detail=error_detail,
            bars=bars,
        )

    @classmethod
    def _build_stock_bars(
        cls,
        results: Sequence[FetchResult],
        instrument: str,
        end: date,
        fetched_at: datetime,
    ) -> tuple[SourceBar, ...]:
        daily, factor, limit, suspend, st = (cls._rows(item) for item in results)
        factors = {
            datetime.strptime(str(row["trade_date"]), "%Y%m%d").date(): float(
                str(row["adj_factor"])
            )
            for row in factor
        }
        if daily and not factors:
            raise ValueError("adj_factor 为空，禁止生成 qfq")
        latest_factor = factors[max(factors)] if factors else 1.0
        limits = {str(row["trade_date"]): row for row in limit}
        suspended = {str(row["trade_date"]) for row in suspend}
        st_dates = {str(row["trade_date"]) for row in st}
        factor_version = hashlib.sha256(
            json.dumps(factor, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]
        result: list[SourceBar] = []
        daily_by_date = {
            datetime.strptime(str(row["trade_date"]), "%Y%m%d").date(): row
            for row in daily
        }
        for row in sorted(daily, key=lambda item: str(item["trade_date"])):
            day_text = str(row["trade_date"])
            trade_date = datetime.strptime(day_text, "%Y%m%d").date()
            factor_value = factors.get(trade_date)
            if factor_value is None:
                raise ValueError(f"{trade_date} 缺少 adj_factor")
            limit_row = limits.get(day_text, {})
            up_limit = _float(limit_row.get("up_limit"))
            down_limit = _float(limit_row.get("down_limit"))
            raw_prices = {
                key: float(str(row[key])) for key in ("open", "high", "low", "close")
            }
            status = TradingStatus.NORMAL
            if day_text in suspended:
                status = TradingStatus.SUSPENDED
            elif up_limit is not None and all(
                abs(raw_prices[key] - up_limit) <= 0.001
                for key in ("open", "high", "low", "close")
            ):
                status = TradingStatus.LIMIT_UP
            elif down_limit is not None and all(
                abs(raw_prices[key] - down_limit) <= 0.001
                for key in ("open", "high", "low", "close")
            ):
                status = TradingStatus.LIMIT_DOWN
            raw_hash = results[0].raw_hash
            for adjustment in (BarAdjustment.RAW, BarAdjustment.QFQ):
                multiplier = (
                    1.0
                    if adjustment is BarAdjustment.RAW
                    else factor_value / latest_factor
                )
                prices = {key: value * multiplier for key, value in raw_prices.items()}
                result.append(
                    SourceBar(
                        instrument_id=instrument,
                        trade_date=trade_date,
                        adjustment=adjustment,
                        open=prices["open"],
                        high=prices["high"],
                        low=prices["low"],
                        close=prices["close"],
                        volume=round(float(str(row["vol"])) * 100),
                        amount=float(str(row["amount"])) * 1000,
                        pre_close=float(str(row["pre_close"])) * multiplier,
                        turnover_rate=None,
                        source_id=f"tushare_stock_{adjustment.value}",
                        upstream_domain="tushare.pro",
                        available_at=fetched_at,
                        raw_hash=raw_hash,
                        adjustment_date=end,
                        adjustment_version=factor_version,
                        trading_status=status,
                        up_limit=up_limit,
                        down_limit=down_limit,
                        is_st=day_text in st_dates,
                    )
                )
        # 停牌日没有 daily K 线，但回测仍必须看见“不可成交”这一事实。
        # 使用停牌前最后一根 raw 收盘构造零成交状态行；它只表达交易约束，
        # 不参与收益计算，且 Gold 会标记为官方单源停牌证据。
        daily_dates = sorted(daily_by_date)
        factor_dates = sorted(factors)
        for day_text in sorted(suspended):
            suspended_day = datetime.strptime(day_text, "%Y%m%d").date()
            if suspended_day in daily_by_date:
                continue
            previous_days = [item for item in daily_dates if item < suspended_day]
            previous_factors = [item for item in factor_dates if item <= suspended_day]
            if not previous_days or not previous_factors:
                raise ValueError(f"{suspended_day} 停牌但缺少停牌前价格/复权因子")
            prior = daily_by_date[max(previous_days)]
            raw_close = float(str(prior["close"]))
            factor_value = factors[max(previous_factors)]
            raw_hash = results[3].raw_hash
            for adjustment in (BarAdjustment.RAW, BarAdjustment.QFQ):
                multiplier = (
                    1.0
                    if adjustment is BarAdjustment.RAW
                    else factor_value / latest_factor
                )
                close = raw_close * multiplier
                result.append(
                    SourceBar(
                        instrument_id=instrument,
                        trade_date=suspended_day,
                        adjustment=adjustment,
                        open=close,
                        high=close,
                        low=close,
                        close=close,
                        volume=0,
                        amount=0.0,
                        pre_close=close,
                        turnover_rate=0.0,
                        source_id=f"tushare_stock_{adjustment.value}",
                        upstream_domain="tushare.pro",
                        available_at=fetched_at,
                        raw_hash=raw_hash,
                        adjustment_date=end,
                        adjustment_version=factor_version,
                        trading_status=TradingStatus.SUSPENDED,
                        is_st=day_text in st_dates,
                    )
                )
        return tuple(
            sorted(result, key=lambda item: (item.trade_date, item.adjustment))
        )


class MockBarProvider:
    """仅供显式离线验收使用；正式模式拒绝注册该 Provider。"""

    supports_adjustments: tuple[BarAdjustment, ...] = (
        BarAdjustment.RAW,
        BarAdjustment.QFQ,
    )

    def __init__(
        self,
        source_id: str,
        upstream_domain: str,
        *,
        price_offset: float = 0.0,
    ) -> None:
        self.source_id = source_id
        self.upstream_domain = upstream_domain
        self._price_offset = price_offset

    def fetch_daily_bars(
        self,
        instruments: Sequence[str],
        start: date,
        end: date,
        adjustment: BarAdjustment,
    ) -> Sequence[FetchResult]:
        payload = json.dumps(
            {
                "source": self.source_id,
                "instruments": sorted(instruments),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": adjustment.value,
            },
            sort_keys=True,
        ).encode()
        raw_hash = hashlib.sha256(payload).hexdigest()
        fetched_at = datetime(2020, 1, 1, tzinfo=UTC)
        bars: list[SourceBar] = []
        for instrument in sorted(normalize_instrument(item) for item in instruments):
            current = start
            index = 0
            while current <= end:
                if current.weekday() < 5:
                    digest = hashlib.sha256(f"{instrument}:{current}".encode()).digest()
                    base = 1.0 + int.from_bytes(digest[:2], "big") / 65535
                    close = base * (1 + index * 0.0001) + self._price_offset
                    bars.append(
                        SourceBar(
                            instrument_id=instrument,
                            trade_date=current,
                            adjustment=adjustment,
                            open=round(close * 0.999, 3),
                            high=round(close * 1.005, 3),
                            low=round(close * 0.995, 3),
                            close=round(close, 3),
                            volume=1_000_000,
                            amount=round(close * 1_000_000, 2),
                            pre_close=None,
                            turnover_rate=None,
                            source_id=self.source_id,
                            upstream_domain=self.upstream_domain,
                            available_at=fetched_at,
                            raw_hash=raw_hash,
                            adjustment_date=end,
                            adjustment_version=raw_hash[:16],
                        )
                    )
                    index += 1
                current += timedelta(days=1)
        return (
            FetchResult(
                source_id=self.source_id,
                upstream_domain=self.upstream_domain,
                dataset="etf_daily_bar",
                fetched_at=fetched_at,
                request_params={"offline": True},
                status_code=200,
                content_type="application/json",
                content=payload,
                state=FetchState.SUCCESS if bars else FetchState.NO_DATA,
                bars=tuple(bars),
            ),
        )
