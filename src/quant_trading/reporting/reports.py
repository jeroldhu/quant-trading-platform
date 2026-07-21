"""报告结果的最小结构。"""

# TODO(P4-REPORT-01): 实现四类报告、图表、版本元数据和内容哈希。
# Contract: docs/development-todo.md#p4-report-01

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReportArtifact:
    report_type: str
    path: Path
    content_sha256: str
