"""完整配置模型——Pipeline、Universe、Backtest、Reporting、Strategy。

所有 YAML 配置在此集中校验，加载阶段不执行采集、建库等副作用。
"""

from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """配置文件缺失、格式错误、字段非法或跨字段校验失败。"""


# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------


class AppSettings(BaseSettings):
    """可由环境变量覆盖的应用级设置。密钥只从环境变量读取。"""

    model_config = SettingsConfigDict(
        env_prefix="QUANT_", env_file=".env", extra="ignore"
    )

    data_root: Path = Path("data")
    snapshot_root: Path = Path("snapshots")
    market_mode: Literal["snapshot", "live"] = "snapshot"
    log_level: str = "INFO"
    remote_ssh_host: str = "aliyun"
    remote_snapshot_root: str = "/srv/quant-trading/snapshots"
    postgres_dsn: str | None = None
    request_timeout_seconds: float = Field(default=20.0, gt=0)
    requests_per_second: float = Field(default=1.0, gt=0)
    min_free_gb: float = Field(default=1.0, ge=0)
    tushare_token: str | None = None


# ---------------------------------------------------------------------------
# Pipeline 配置
# ---------------------------------------------------------------------------


class PipelineCollectionConfig(BaseModel):
    """数据采集范围。"""

    model_config = ConfigDict(extra="forbid")

    asset_types: list[str] = Field(default_factory=lambda: ["etf"])
    scope: Literal["full_market", "candidates_only"] = "full_market"
    exchanges: list[str] = Field(default_factory=lambda: ["SSE", "SZSE"])
    include_delisted: bool = True
    adjustments: list[Literal["raw", "qfq"]] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    benchmarks: list[str] = Field(default_factory=lambda: ["000300.SH"])
    configured_instruments: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_collection_scope(self) -> "PipelineCollectionConfig":
        if "etf_daily_bar" in self.datasets and set(self.adjustments) != {"raw", "qfq"}:
            raise ValueError("ETF 日线必须同时配置 raw 和 qfq")
        if self.scope == "candidates_only" and not self.configured_instruments:
            raise ValueError("candidates_only 必须配置 configured_instruments")
        return self


class PipelineHistoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = "2015-01-01"
    end: str = "latest_trade_date"
    respect_listing_date: bool = True

    @model_validator(mode="after")
    def _check_history_range(self) -> "PipelineHistoryConfig":
        try:
            start_date = date.fromisoformat(self.start)
        except ValueError as exc:
            raise ValueError("history.start 必须是 YYYY-MM-DD") from exc
        if self.end not in {"latest", "latest_trade_date"}:
            try:
                end_date = date.fromisoformat(self.end)
            except ValueError as exc:
                raise ValueError(
                    "history.end 必须是 YYYY-MM-DD/latest/latest_trade_date"
                ) from exc
            if start_date > end_date:
                raise ValueError("history.start 不能晚于 history.end")
        return self


class PipelineIncrementalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlap_trade_days: int = Field(default=5, ge=0)


class PipelineQualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_publish_coverage: float = Field(default=0.99, gt=0, le=1)
    min_independent_sources: int = Field(default=2, ge=2)

    @model_validator(mode="after")
    def _check_min_sources(self) -> "PipelineQualityConfig":
        if self.min_independent_sources < 2:
            raise ValueError("min_independent_sources 至少为 2")
        return self


class PipelineSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    required: bool = False
    fallback_ttl_hours: int | None = Field(default=None, ge=0)
    max_stale_days: int | None = Field(default=None, ge=0)
    note: str = ""


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: PipelineCollectionConfig = Field(
        default_factory=lambda: PipelineCollectionConfig(
            datasets=["etf_master", "etf_daily_bar", "index_daily", "trade_calendar"]
        )
    )
    history: PipelineHistoryConfig = PipelineHistoryConfig()
    incremental: PipelineIncrementalConfig = PipelineIncrementalConfig()
    quality: PipelineQualityConfig = PipelineQualityConfig()
    sources: dict[str, PipelineSourceConfig] = Field(
        default_factory=lambda: {
            "eastmoney": PipelineSourceConfig(required=True),
            "tencent": PipelineSourceConfig(required=False),
            "ths": PipelineSourceConfig(required=True),
            "tdx": PipelineSourceConfig(required=True),
            "baostock": PipelineSourceConfig(required=False),
            "tushare": PipelineSourceConfig(required=False),
            "akshare": PipelineSourceConfig(required=False),
        }
    )


# ---------------------------------------------------------------------------
# 交易所日历配置
# ---------------------------------------------------------------------------


class CalendarSourceConfig(BaseModel):
    """一份交易所年度休市公告。"""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    exchange: Literal["SSE", "SZSE"]
    upstream_domain: str
    version: str
    url: str
    available_at: datetime


class CalendarClosureConfig(BaseModel):
    """闭区间休市日期。周末无需重复配置。"""

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    reason: str

    @model_validator(mode="after")
    def _check_range(self) -> "CalendarClosureConfig":
        if self.start > self.end:
            raise ValueError("休市区间 start 不能晚于 end")
        if self.start.year != self.end.year:
            raise ValueError("单个休市区间不能跨年")
        return self


class CalendarYearConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: tuple[CalendarSourceConfig, ...]
    closures: tuple[CalendarClosureConfig, ...]

    @model_validator(mode="after")
    def _check_sources(self) -> "CalendarYearConfig":
        domains = {source.upstream_domain for source in self.sources}
        exchanges = {source.exchange for source in self.sources}
        if len(domains) < 2 or exchanges != {"SSE", "SZSE"}:
            raise ValueError("正式交易日历必须同时有 SSE/SZSE 两个独立官方来源")
        return self


class TradingCalendarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    years: dict[int, CalendarYearConfig]

    @model_validator(mode="after")
    def _check_years(self) -> "TradingCalendarConfig":
        for year, entry in self.years.items():
            if any(item.start.year != year for item in entry.closures):
                raise ValueError(f"{year} 年配置包含其他年份的休市区间")
        return self


# ---------------------------------------------------------------------------
# Universe 配置
# ---------------------------------------------------------------------------


class UniverseEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["configured", "full_market", "theme_snapshot"]
    instruments: list[str] = Field(default_factory=list)
    board_types: list[Literal["concept", "industry"]] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_mode_fields(self) -> "UniverseEntry":
        if self.mode == "configured" and not self.instruments:
            raise ValueError("configured 资产池必须提供 instruments")
        if self.mode == "theme_snapshot" and not self.board_types:
            raise ValueError("theme_snapshot 资产池必须提供 board_types")
        return self


class UniverseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    benchmarks: dict[str, str] = Field(default_factory=dict)
    universes: dict[str, UniverseEntry]


# ---------------------------------------------------------------------------
# Backtest 配置
# ---------------------------------------------------------------------------


class BacktestConfig(BaseModel):
    """回测参数。费用不能静默默认。"""

    model_config = ConfigDict(extra="forbid")

    initial_cash: float = Field(gt=0)
    commission_rate: float = Field(ge=0)
    minimum_commission: float = Field(ge=0)
    stock_sell_stamp_duty_rate: float = Field(ge=0)
    slippage_rate: float = Field(ge=0)
    lot_size: int = Field(default=100, ge=1)
    max_volume_participation: float = Field(gt=0, le=1)
    signal_price_adjustment: Literal["qfq"] = "qfq"
    execution_price_adjustment: Literal["raw"] = "raw"

    @model_validator(mode="after")
    def _check_adjustments(self) -> "BacktestConfig":
        if self.signal_price_adjustment != "qfq":
            raise ValueError("信号价格必须使用 qfq")
        if self.execution_price_adjustment != "raw":
            raise ValueError("成交价格必须使用 raw")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash 必须大于 0")
        return self


# ---------------------------------------------------------------------------
# Reporting / AI 配置
# ---------------------------------------------------------------------------


class AIEvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    prompt_version: str = "quant-readonly-v1"
    timeout_seconds: int = Field(default=120, ge=1)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_input_chars: int = Field(default=30_000, ge=1_000)
    max_completion_tokens: int = Field(default=1_500, ge=100, le=8_000)
    max_total_tokens_per_evaluation: int = Field(default=8_000, ge=500)
    dimensions: list[Literal["backtest", "signal", "signal_explanation", "anomaly"]] = (
        Field(default_factory=list)
    )


class ReportingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai: AIEvaluationConfig = AIEvaluationConfig()


# ---------------------------------------------------------------------------
# Strategy 公共字段
# ---------------------------------------------------------------------------


class StrategyFile(BaseModel):
    """策略 YAML 公共字段；parameters 由具体策略二次校验。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    version: str
    capital_weight: float = Field(ge=0.0, le=1.0)
    required_readiness: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# YAML 加载
# ---------------------------------------------------------------------------


def _raw_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败: {path}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"配置根节点必须是映射: {path}")
    return loaded


def load_pipeline_config(path: Path = Path("configs/pipeline.yaml")) -> PipelineConfig:
    return PipelineConfig.model_validate(_raw_yaml(path))


def load_trading_calendar_config(
    path: Path = Path("configs/trading_calendar.yaml"),
) -> TradingCalendarConfig:
    return TradingCalendarConfig.model_validate(_raw_yaml(path))


def load_universe_config(
    path: Path = Path("configs/universes.yaml"),
) -> UniverseConfig:
    return UniverseConfig.model_validate(_raw_yaml(path))


def load_backtest_config(
    path: Path = Path("configs/backtest.yaml"),
) -> BacktestConfig:
    return BacktestConfig.model_validate(_raw_yaml(path))


def load_reporting_config(
    path: Path = Path("configs/reporting.yaml"),
) -> ReportingConfig:
    return ReportingConfig.model_validate(_raw_yaml(path))


def load_strategy_file(path: Path) -> StrategyFile:
    return StrategyFile.model_validate(_raw_yaml(path))
