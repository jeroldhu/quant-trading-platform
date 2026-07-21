"""报告结果的最小结构。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReportArtifact:
    report_type: str
    path: Path
    content_sha256: str
