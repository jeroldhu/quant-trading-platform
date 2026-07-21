"""内置策略显式清单。

不要改为目录扫描或依赖 import 副作用；清单必须能被代码审查直接看到。
"""

from collections.abc import Callable

from quant_trading.config import StrategyFile
from quant_trading.research.strategy import Strategy

from .cross_sectional import CrossSectionalStrategy
from .etf_rotation import EtfRotationStrategy
from .theme_rotation import ThemeRotationStrategy

StrategyFactory = Callable[[StrategyFile], Strategy]

BUILTIN_STRATEGIES: dict[str, StrategyFactory] = {
    EtfRotationStrategy.name: EtfRotationStrategy.from_config,
    ThemeRotationStrategy.name: ThemeRotationStrategy.from_config,
    CrossSectionalStrategy.name: CrossSectionalStrategy.from_config,
}
