"""Point-in-time 资产池工具。"""

from collections.abc import Iterable


def normalize_universe(instruments: Iterable[str]) -> tuple[str, ...]:
    """去重并稳定排序，保证相同输入产生确定结果。"""

    normalized = {instrument.strip().upper() for instrument in instruments}
    return tuple(sorted(instrument for instrument in normalized if instrument))
