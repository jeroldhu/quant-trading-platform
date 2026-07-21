"""策略注册、创建与配置校验。"""

# TODO(P3-RESEARCH-04): 支持 enabled 策略批量创建、资金校验和版本清单。
# Contract: docs/development-todo.md#p3-research-04

from quant_trading.config import StrategyFile

from .strategies import BUILTIN_STRATEGIES
from .strategy import Strategy


class StrategyRegistryError(ValueError):
    """策略未注册、重复或配置名称不匹配。"""


def list_strategy_names() -> tuple[str, ...]:
    """稳定排序，便于 CLI、测试和文档生成。"""

    return tuple(sorted(BUILTIN_STRATEGIES))


def create_strategy(config: StrategyFile) -> Strategy:
    """按显式注册表创建策略，并让策略校验自己的 parameters。"""

    try:
        factory = BUILTIN_STRATEGIES[config.name]
    except KeyError as error:
        available = ", ".join(list_strategy_names())
        raise StrategyRegistryError(
            f"未知策略 {config.name!r}；可用策略: {available}"
        ) from error
    return factory(config)
