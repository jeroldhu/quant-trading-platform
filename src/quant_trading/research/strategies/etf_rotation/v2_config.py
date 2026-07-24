"""ETF 行业轮动 V2 策略参数配置。

所有参数集中管理，支持 Pydantic 验证和 JSON 序列化。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class V2StrategyConfig(BaseModel):
    """V2 ETF 轮动策略完整参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ---- 第一层：基础过滤 ----
    min_listing_days: int = Field(default=120, ge=60, description="最少上市交易日数")
    min_amount_ma20: float = Field(default=50_000_000, ge=0, description="20 日最低平均成交额")

    # ---- 第二层：因子参数 ----
    ma_short: int = Field(default=20, ge=5, le=60, description="短期均线周期")
    ma_long: int = Field(default=60, ge=20, le=250, description="长期均线周期")
    ma_slope_lookback: int = Field(default=20, ge=5, le=60, description="MA60 斜率回看窗口")
    overextension_atr_mult: float = Field(default=1.5, ge=0.5, le=5.0, description="过度偏离 ATR 倍数阈值")

    # ---- 第三层：评分权重 ----
    weight_m20: float = Field(default=0.30, ge=0.0, le=1.0)
    weight_m60: float = Field(default=0.30, ge=0.0, le=1.0)
    weight_tq: float = Field(default=0.20, ge=0.0, le=1.0)
    weight_vol: float = Field(default=0.10, ge=0.0, le=0.5)
    weight_mdd: float = Field(default=0.05, ge=0.0, le=0.3)
    weight_ext: float = Field(default=0.05, ge=0.0, le=0.3)

    # ---- 第四层：标的选择 ----
    top_n: int = Field(default=2, ge=1, le=5, description="持仓数量")

    # ---- 第五层：仓位分配 ----
    max_single_weight: float = Field(default=0.40, ge=0.0, le=1.0, description="单只 ETF 仓位上限")
    normal_total_weight: float = Field(default=0.80, ge=0.0, le=1.0, description="正常目标总仓位")

    # ---- 第六层：市场风险开关 ----
    market_bull_total: float = Field(default=0.80, ge=0.0, le=1.0)
    market_weak_total: float = Field(default=0.50, ge=0.0, le=1.0)
    market_bear_total: float = Field(default=0.20, ge=0.0, le=1.0)

    # ---- 第七层：组合回撤控制 ----
    dd_warning_level: float = Field(default=0.08, ge=0.01, le=0.50, description="回撤警告线")
    dd_warning_cap: float = Field(default=0.60, ge=0.0, le=1.0, description="回撤警告仓位上限")
    dd_severe_level: float = Field(default=0.12, ge=0.01, le=0.50, description="回撤严重线")
    dd_severe_cap: float = Field(default=0.30, ge=0.0, le=1.0, description="回撤严重仓位上限")
    dd_crash_level: float = Field(default=0.15, ge=0.01, le=0.50, description="回撤崩溃线")
    cooldown_days: int = Field(default=10, ge=1, le=60, description="冷静期交易日数")

    # ---- 第八层：回撤恢复 ----
    dd_recovery_margin: float = Field(default=0.02, ge=0.0, le=0.20, description="恢复所需回撤下降幅度")
    dd_recovery_confirm_days: int = Field(default=3, ge=1, le=10, description="恢复确认天数")

    # ---- 第九层：换仓控制 ----
    turnover_threshold: float = Field(default=0.05, ge=0.0, le=0.30, description="换仓得分阈值")
    min_holding_days: int = Field(default=5, ge=0, le=30, description="最小持仓周期(交易日)")

    # ---- 第十层：调仓频率 ----
    rebalance_frequency: str = Field(default="weekly", description="调仓频率: daily / weekly")

    # ---- 第十一层：交易成本 ----
    buy_commission: float = Field(default=0.0003, ge=0.0, le=0.01, description="买入佣金率")
    sell_commission: float = Field(default=0.0003, ge=0.0, le=0.01, description="卖出佣金率")
    slippage: float = Field(default=0.0005, ge=0.0, le=0.01, description="滑点率")
    min_commission: float = Field(default=5.0, ge=0.0, description="最低佣金")
    etf_stamp_duty: float = Field(default=0.0, ge=0.0, description="ETF 印花税(通常为 0)")
    lot_size: int = Field(default=100, ge=1, description="最小交易手数")
    max_volume_participation: float = Field(default=0.10, ge=0.0, le=1.0, description="最大成交量参与比例")

    # ---- 第十二层：初始资金 ----
    initial_cash: float = Field(default=1_000_000.0, ge=1.0, description="初始资金")


# 默认配置实例
DEFAULT_CONFIG = V2StrategyConfig()
