"""回测引擎最小契约。

首期只固定不可妥协的价格边界；事件时钟、撮合和账本将在后续实现。
"""

from dataclasses import dataclass

from quant_trading.models import BarAdjustment


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_cash: float
    signal_adjustment: BarAdjustment = BarAdjustment.QFQ
    execution_adjustment: BarAdjustment = BarAdjustment.RAW

    def validate_price_contract(self) -> None:
        """禁止用复权价格成交，也禁止用未复权价格直接产生跨期信号。"""

        if self.signal_adjustment is not BarAdjustment.QFQ:
            raise ValueError("信号价格必须使用 qfq")
        if self.execution_adjustment is not BarAdjustment.RAW:
            raise ValueError("成交价格必须使用 raw")
