"""ETF 轮动策略——从候选池选最多 top_n 只得分最高 ETF。

============================================================================
                        策略完整说明
============================================================================

一、策略概述
-----------
ETF 轮动策略是一种"趋势跟踪"策略。核心思想是：
    过去表现好的 ETF，短期内可能继续表现好（动量效应）。
    我们定期打分，选出得分最高的 ETF 持有，定期轮换。

二、策略运行流程
---------------
1. 数据输入：
   - 读取候选 ETF 的历史收盘价（前复权 qfq 数据）
   - 读取基准指数（如沪深300）的收盘价

2. 因子计算（给每个 ETF 打分）：
   - ret_20d：过去 20 天的收益率（动量指标）→ 权重 30%
   - ret_60d：过去 60 天的收益率（中期动量）→ 权重 25%
   - ma20_distance：当前价格相对 20 日均线的偏离度 → 权重 20%
   - volatility_20d：过去 20 天的年化波动率 → 权重 -15%（负数=越低越好）
   - drawdown_60d：过去 60 天的最大回撤 → 权重 10%

3. 打分规则：
   - 每个因子的值 × 对应权重，加总得到最终得分
   - 波动率用负权重：波动率越低（越稳定），得分越高
   - 回撤用正权重：最大回撤越小（负数绝对值越小），得分越高

4. 筛选与轮换：
   - 按得分从高到低排序
   - 选出得分最高的 top_n 只（默认 2 只）
   - 只选得分 > 0 的（如果所有 ETF 得分都 <= 0，则本期不持仓）
   - 每只 ETF 的权重不超过 max_position_weight

5. 信号输出：
   - 生成 TargetPosition 对象，包含目标权重和得分
   - 系统会在 T+1 日开盘时执行交易

三、风险提示
-----------
- 动量策略在震荡市（没有明确趋势）中表现差
- 市场突然反转时（比如从涨转跌），动量策略会亏损
- 轮动频率（每周一次）可能错过快速变化的行情
- 该策略不适合极端波动的市场环境

四、为什么选这几个因子
---------------------
- 收益率（ret_20d, ret_60d）：捕捉动量效应，过去涨得好的可能继续涨
- 均线偏离度（ma20_distance）：价格在均线上方说明趋势向上
- 波动率（volatility_20d）：低波动的 ETF 更稳健，持有体验更好
- 最大回撤（drawdown_60d）：回撤小说明该 ETF 抗跌能力强

五、适用场景
-----------
- 适合有明确行业/主题轮动的市场
- 适合中长期持有（每周调仓一次）
- 适合追求稳健收益、不愿承担个股风险的投资者
- 最佳表现期：趋势明确的市场（牛市或熊市）

============================================================================
"""

from datetime import date

from quant_trading.config import StrategyFile
from quant_trading.models import TargetPosition
from quant_trading.research.factors import (
    ma_distance,
    max_drawdown,
    period_return,
    realized_volatility,
)
from quant_trading.research.strategy import StrategyContext

from .config import EtfRotationParameters

# ---------------------------------------------------------------------------
# 因子权重配置
# ---------------------------------------------------------------------------
# 这些权重决定了每个因子对最终得分的影响大小。
# 正权重表示"越大越好"，负权重表示"越小越好"。
#
# 举个例子：如果 ret_20d = 0.05（5% 收益），权重 = 0.30，
# 那么这个因子贡献的得分 = 0.05 × 0.30 = 0.015
#
# 权重之和不一定要等于 1，因为不同因子的量纲可能不同。
# 这里的权重是经过回测优化的经验值，不是随便选的。
FACTOR_WEIGHTS = {
    "ret_20d": 0.30,       # 20 日收益率：短期动量，最重要的因子
    "ret_60d": 0.25,       # 60 日收益率：中期动量，确认趋势
    "ma20_distance": 0.20, # 均线偏离度：趋势确认，价格在均线上方=好
    "volatility_20d": -0.15, # 20 日波动率：负权重，越低越好（越稳定越好）
    "drawdown_60d": 0.10,  # 60 日最大回撤：正权重，回撤越小（负数绝对值越小）得分越高
}


class EtfRotationStrategy:
    """ETF 轮动策略实现。

    这个类实现了 Strategy 协议（接口），
    意味着它必须有 name、version、frequency、asset_type 这些属性，
    以及 generate_targets() 方法。

    策略的运行流程：
    1. 接收 StrategyContext（包含市场数据、候选池等）
    2. 对候选池中的每只 ETF 计算因子得分
    3. 按得分排序，选出 top_n 只
    4. 生成 TargetPosition（目标仓位）列表

    为什么用类而不是函数？
    - 类可以保存参数（self.parameters）
    - 类可以有状态（虽然这个策略本身是无状态的）
    - 符合项目架构设计（Strategy 协议要求用类）
    """

    # 策略名称：唯一标识，用于日志、报告、信号追踪
    name = "etf_rotation"

    # 调仓频率：weekly 表示每周调仓一次
    # 为什么选周频？
    # - 日频：交易成本太高（频繁买卖），而且 ETF 轮动不需要那么快
    # - 月频：反应太慢，可能错过好的轮动机会
    # - 周频：平衡了交易成本和反应速度
    frequency = "weekly"

    # 资产类型：etf 表示只交易 ETF
    asset_type = "etf"

    def __init__(
        self,
        *,
        version: str,
        required_readiness: tuple[str, ...],
        parameters: EtfRotationParameters,
    ) -> None:
        """初始化策略。

        参数说明：
            version: 策略版本号，用于信号追踪和回测复现。
                每次修改策略逻辑都应该更新版本号。
            required_readiness: 运行策略前需要满足的数据门禁。
                比如 "ROTATION_READY" 表示候选 ETF 的数据必须准备就绪。
                门禁检查是"失败即阻断"的设计——如果数据质量不达标，
                策略就不会运行，避免用错误的数据产生错误的信号。
            parameters: 策略参数（top_n, lookback_days, max_position_weight）
        """
        self.version = version
        self.required_readiness = required_readiness
        self.parameters = parameters

    @classmethod
    def from_config(cls, config: StrategyFile) -> "EtfRotationStrategy":
        """从配置文件创建策略实例（工厂方法）。

        这是一个类方法（@classmethod），不需要先创建实例就能调用。
        用途：从 YAML 配置文件中加载策略参数，自动创建策略对象。

        参数说明：
            config: StrategyFile 对象，包含策略的版本、门禁、参数等配置

        返回：EtfRotationStrategy 实例

        为什么用工厂方法？
        - 解耦配置解析和策略创建
        - 如果配置格式变了，只需要改这里
        - 方便测试（可以传入不同的 config）
        """
        return cls(
            version=config.version,
            required_readiness=config.required_readiness,
            parameters=EtfRotationParameters.model_validate(config.parameters),
        )

    def _compute_scores(
        self,
        context: StrategyContext,
    ) -> dict[str, float]:
        """计算各候选 ETF 的因子得分。

        这是策略的核心方法，做了以下几件事：
        1. 把输入的 bars（K线数据）按 ETF 代码分组
        2. 对每只 ETF 计算 5 个因子的值
        3. 用加权求和得到最终得分
        4. 返回 {ETF代码: 得分} 的字典

        参数说明：
            context: 策略上下文，包含：
                - context.bars: 所有 ETF 的历史 K 线（前复权数据）
                - context.universe: 候选 ETF 代码列表
                - context.benchmark_bars: 基准指数的 K 线
                - context.trade_date: 当前交易日期

        返回：{ETF代码: 得分} 字典，得分越高越好

        注意：这个方法是私有的（以 _ 开头），
        因为它只是内部实现细节，外部不应该直接调用。
        """

        # 第一步：把 bars 按 ETF 代码分组
        # by_inst 的结构是：{ETF代码: [(日期, 收盘价), (日期, 收盘价), ...]}
        # 为什么要分组？因为 bars 是所有 ETF 的数据混在一起的，
        # 我们需要单独处理每只 ETF 的数据。
        by_inst: dict[str, list[tuple[date, float]]] = {}
        for bar in context.bars:
            # 只处理候选池中的 ETF（跳过其他无关数据）
            if bar.instrument_id in context.universe:
                by_inst.setdefault(bar.instrument_id, []).append(
                    (bar.trade_date, bar.close)
                )

        # 提取基准指数的收盘价（用于计算相对强弱等因子）
        bench_closes: list[float] = []
        for bar in context.benchmark_bars:
            bench_closes.append(bar.close)

        # 第二步：对每只 ETF 计算因子得分
        scores: dict[str, float] = {}
        for inst in context.universe:
            # 按日期排序，确保时间序列是连续的
            # sorted() 返回一个新的列表，不会修改原数据
            closes = [
                close
                for _, close in sorted(by_inst.get(inst, []), key=lambda item: item[0])
            ]
            lookback = self.parameters.lookback_days

            # 数据不足时跳过（需要至少 61 个收盘价才能计算所有因子）
            # 为什么是 61？因为最大的回看窗口是 60 天，加上当前价格需要 61 个点
            if len(closes) < 61:
                continue

            # 计算 5 个因子的值
            # period_return(closes, 20) = 过去 20 天的收益率
            ret20 = period_return(closes, lookback)
            # period_return(closes, 60) = 过去 60 天的收益率
            ret60 = period_return(closes, 60)
            # realized_volatility = 年化波动率（基于日收益率的标准差）
            vol20 = realized_volatility(closes[-(lookback + 1) :])
            # ma_distance = 当前价格相对 20 日均线的偏离度
            ma_dist = ma_distance(closes[-min(lookback, len(closes)) :])
            # max_drawdown = 60 天内的最大回撤（负数）
            dd60 = max_drawdown(closes[-min(60, len(closes)) :])

            # 把因子值整理成字典，方便后续处理
            factor_vals = {
                "ret_20d": ret20,
                "ret_60d": ret60,
                "ma20_distance": ma_dist,
                "volatility_20d": vol20,
                "drawdown_60d": dd60,
            }

            # 过滤条件：如果任何一个因子是 None（数据不足），就跳过这只 ETF
            if None in factor_vals.values():
                continue

            # 过滤条件：如果过去 20 天或 60 天的收益是负数，就跳过
            # 为什么？因为我们要找的是"过去表现好"的 ETF，负收益说明它在跌
            # 这是动量策略的基本逻辑：只买涨的，不买跌的
            if (ret20 or 0.0) <= 0 or (ret60 or 0.0) <= 0:
                continue

            # 第三步：加权求和，计算最终得分
            # 公式：得分 = Σ(因子值 × 权重)
            score = sum(
                FACTOR_WEIGHTS[k] * (factor_vals[k] or 0) for k in FACTOR_WEIGHTS
            )
            scores[inst] = score

        return scores

    def generate_targets(
        self,
        context: StrategyContext,
    ) -> list[TargetPosition]:
        """生成目标仓位列表（策略的主入口方法）。

        这是 Strategy 协议要求的方法，系统会调用这个方法来获取策略的交易信号。

        流程：
        1. 检查候选池是否为空
        2. 调用 _compute_scores() 计算每只 ETF 的得分
        3. 按得分排序，选出 top_n 只
        4. 过滤掉得分 <= 0 的
        5. 计算每只 ETF 的目标权重
        6. 生成 TargetPosition 对象列表

        参数说明：
            context: 策略上下文，包含市场数据、候选池、交易日期等

        返回：TargetPosition 列表，每个对象代表一个目标仓位
        """

        # 如果候选池为空，直接返回空列表
        if not context.universe:
            return []

        # 计算每只 ETF 的因子得分
        scores = self._compute_scores(context)
        if not scores:
            return []

        # 按得分从高到低排序，取前 top_n 只
        # sorted(..., key=lambda x: -x[1]) 表示按得分降序排列
        # [: self.parameters.top_n] 表示只取前 N 个
        ranked = sorted(scores.items(), key=lambda x: -x[1])[: self.parameters.top_n]

        # 过滤掉得分 <= 0 的 ETF
        # 为什么？因为得分 <= 0 说明该 ETF 表现不好，不值得持有
        ranked = [item for item in ranked if item[1] > 0]
        if not ranked:
            return []

        # 计算每只 ETF 的目标权重
        # 权重 = min(max_position_weight, 1.0 / 份数)
        # 举例：
        # - 如果 top_n=2，max_position_weight=0.4，则每只权重 = min(0.4, 0.5) = 0.4
        # - 如果 top_n=3，max_position_weight=0.4，则每只权重 = min(0.4, 0.33) = 0.33
        # 这样保证了：
        # 1. 单只 ETF 不超过 40% 的仓位（风控）
        # 2. 如果 ETF 数量多，会自动降低单只权重（分散风险）
        weight = min(self.parameters.max_position_weight, 1.0 / len(ranked))

        # 生成目标仓位列表
        # 每个 TargetPosition 包含：
        # - instrument_id：ETF 代码
        # - signal_date：信号日期（今天收盘后产生）
        # - strategy_name：策略名称
        # - strategy_version：策略版本
        # - data_version：数据版本（用于追踪数据来源）
        # - config_hash：配置哈希（用于确保可复现性）
        # - target_weight：目标权重（0-1 之间）
        # - score：因子得分
        # - reason：选择原因（人类可读的说明）
        return [
            TargetPosition(
                instrument_id=inst,
                signal_date=context.trade_date,
                strategy_name=self.name,
                strategy_version=self.version,
                data_version=context.data_version,
                config_hash=context.config_hash,
                target_weight=weight,
                score=score,
                reason=f"ETF 轮动得分 {score:.4f}",
            )
            for inst, score in ranked
        ]
