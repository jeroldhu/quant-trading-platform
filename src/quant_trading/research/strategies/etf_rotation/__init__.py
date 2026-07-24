"""ETF 轮动策略模块。

这个模块实现了一个 ETF（交易型开放式指数基金）轮动策略。
策略的核心思想是：定期从一篮子候选 ETF 中，选出得分最高的几只来持有，
通过"追涨"表现好的 ETF 来获取超额收益。

ETF 轮动 vs 股票轮动的优势：
- ETF 分散了单只股票的风险（一只 ETF 通常包含几十甚至上百只成分股）
- ETF 交易费用低（免印花税，佣金通常更低）
- ETF 流动性好，不存在停牌、退市风险
- 避免了个股的"黑天鹅"事件
"""

from .strategy import EtfRotationStrategy

__all__ = ["EtfRotationStrategy"]
