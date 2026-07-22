"""策略注册、创建与批量加载。

保持显式注册，不使用目录扫描或 import 副作用。
"""

from pathlib import Path

from quant_trading.config import StrategyFile, load_strategy_file

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


def load_enabled_strategies(
    config_dir: Path = Path("configs/strategies"),
) -> dict[str, Strategy]:
    """加载所有 enabled 的策略配置并创建实例。"""
    if not config_dir.is_dir():
        return {}

    strategies: dict[str, Strategy] = {}
    total_weight = 0.0

    for path in sorted(config_dir.glob("*.yaml")):
        config = load_strategy_file(path)
        if not config.enabled:
            continue
        if path.stem != config.name:
            raise StrategyRegistryError(
                f"文件名 {path.stem!r} 与策略名 {config.name!r} 不一致"
            )
        strategy = create_strategy(config)
        strategies[strategy.name] = strategy
        total_weight += config.capital_weight

    if total_weight > 1.0:
        raise StrategyRegistryError(f"capital_weight 合计 {total_weight:.4f} 超过 1.0")

    return strategies


def load_strategy_configs(
    config_dir: Path = Path("configs/strategies"),
) -> dict[str, StrategyFile]:
    """加载所有策略配置文件（含 disabled）。"""
    if not config_dir.is_dir():
        return {}
    configs: dict[str, StrategyFile] = {}
    for path in sorted(config_dir.glob("*.yaml")):
        config = load_strategy_file(path)
        configs[config.name] = config
    return configs
