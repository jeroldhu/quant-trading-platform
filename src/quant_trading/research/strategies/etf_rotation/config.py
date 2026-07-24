"""ETF 轮动策略参数配置。

这个文件定义了策略运行时需要的参数。
所有参数都有默认值和验证规则（比如 top_n 不能超过 2），
这样可以防止配置错误导致策略运行异常。

参数说明（给新手）：
- 在量化交易中，策略参数决定了策略的行为。
- 比如"选几只 ETF"、"看多长时间的历史数据"，这些都是参数。
- 好的参数配置应该有合理的默认值和边界限制。
"""

from pydantic import BaseModel, ConfigDict, Field


class EtfRotationParameters(BaseModel):
    """ETF 轮动策略的参数定义。

    这里用 Pydantic 的 BaseModel 来定义参数，
    好处是：
    1. 自动验证参数类型（比如 top_n 必须是整数）
    2. 自动验证参数范围（比如 top_n 必须在 1-2 之间）
    3. 如果参数不符合要求，会自动抛出清晰的错误信息
    """

    # Pydantic 配置：禁止额外的字段。
    # 意思是：如果你传入了一个这里没定义的参数，会报错。
    # 这样可以防止拼写错误（比如把 top_n 写成 top_N）导致的 bug。
    model_config = ConfigDict(extra="forbid")

    # top_n：每次选几只 ETF 来持有。
    # 默认值是 2，范围是 1-2。
    # 为什么限制最多 2 只？因为轮动策略需要集中持仓，
    # 太多只 ETF 会稀释收益，而且管理起来也更复杂。
    top_n: int = Field(default=2, ge=1, le=2)

    # lookback_days：回看天数，也就是看最近多少个交易日的数据来打分。
    # 默认值是 20（大约一个月的交易日）。
    # 这个参数决定了策略的"眼光"有多远：
    # - 太短（比如 5 天）：容易被短期波动干扰
    # - 太长（比如 60 天）：反应太慢，错过好的轮动机会
    # 20 天是一个比较平衡的选择。
    lookback_days: int = Field(default=20, ge=2)

    # max_position_weight：单只 ETF 的最大持仓权重。
    # 默认值是 0.4（40%），范围是 0-1（但不能等于 0）。
    # 这是风控参数：即使某只 ETF 得分最高，也不能把所有钱都投进去。
    # 40% 的上限意味着至少要分散到 3 只以上的 ETF。
    max_position_weight: float = Field(default=0.4, gt=0, le=1)
