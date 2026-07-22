"""报告生成——daily/weekly/backtest/quality。

每份报告记录 run_id、策略版本、配置哈希、data_version 和门禁状态。
"""

import csv
import hashlib
import html
import io
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ReportArtifact:
    report_type: str
    path: Path
    content_sha256: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


ReportType = Literal["daily", "weekly", "backtest", "quality"]
REQUIRED_METADATA = {
    "run_id",
    "snapshot_id",
    "data_version",
    "strategies",
    "config_hashes",
    "readiness",
}


def validate_report_payload(data: dict[str, Any]) -> None:
    """所有正式报告共享同一组可回放元数据。"""
    missing = sorted(REQUIRED_METADATA - set(data))
    if missing:
        raise ValueError(f"报告缺少审计字段: {', '.join(missing)}")
    if not str(data.get("run_id") or ""):
        raise ValueError("run_id 不能为空")
    if not str(data.get("snapshot_id") or ""):
        raise ValueError("snapshot_id 不能为空")
    if not str(data.get("data_version") or ""):
        raise ValueError("data_version 不能为空")


def _atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    staging.write_text(body, encoding="utf-8")
    os.replace(staging, path)


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------


def write_json_report(
    output_dir: Path,
    prefix: str,
    data: dict[str, Any],
    *,
    trade_date: date | None = None,
) -> ReportArtifact:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}.json"
    path = output_dir / filename
    body = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    _atomic_write(path, body)
    return ReportArtifact(report_type=prefix, path=path, content_sha256=_sha256(body))


def write_markdown_report(
    output_dir: Path,
    prefix: str,
    sections: dict[str, str],
) -> ReportArtifact:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{prefix}.md"
    lines: list[str] = []
    for heading, content in sections.items():
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(content)
        lines.append("")
    body = "\n".join(lines)
    _atomic_write(path, body)
    return ReportArtifact(report_type=prefix, path=path, content_sha256=_sha256(body))


def write_html_report(
    output_dir: Path,
    prefix: str,
    title: str,
    data: dict[str, Any],
) -> ReportArtifact:
    """生成无外部依赖的可归档 HTML。"""
    payload = html.escape(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font:15px/1.6 system-ui;max-width:1100px;margin:40px auto;padding:0 20px}}
pre{{background:#f6f8fa;padding:18px;border-radius:8px;overflow:auto}}</style></head>
<body><h1>{html.escape(title)}</h1><pre>{payload}</pre></body></html>"""
    path = output_dir / f"{prefix}.html"
    _atomic_write(path, body)
    return ReportArtifact(prefix, path, _sha256(body))


def write_equity_svg(
    output_dir: Path,
    prefix: str,
    snapshots: list[dict[str, Any]],
) -> ReportArtifact | None:
    """回测存在至少两个净值点时生成轻量 SVG 净值图。"""
    points = [float(item["net_value"]) for item in snapshots if "net_value" in item]
    if len(points) < 2:
        return None
    width, height, padding = 800, 260, 30
    low, high = min(points), max(points)
    span = high - low or 1.0
    coords = []
    for index, value in enumerate(points):
        x = padding + index * (width - 2 * padding) / (len(points) - 1)
        y = height - padding - (value - low) * (height - 2 * padding) / span
        coords.append(f"{x:.2f},{y:.2f}")
    body = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="white"/>'
        f'<polyline points="{" ".join(coords)}" fill="none" '
        'stroke="#1769aa" stroke-width="2"/></svg>'
    )
    path = output_dir / f"{prefix}-equity.svg"
    _atomic_write(path, body)
    return ReportArtifact(prefix, path, _sha256(body))


def write_trades_csv(
    output_dir: Path, prefix: str, trades: list[dict[str, Any]]
) -> ReportArtifact:
    """输出可直接复核的成交明细；无成交时仍保留表头。"""
    default_fields = (
        "trade_id",
        "order_id",
        "instrument_id",
        "side",
        "quantity",
        "raw_price",
        "commission",
        "minimum_commission",
        "stamp_duty",
        "slippage",
        "total_fee",
        "executed_at",
    )
    extra_fields = sorted(
        {str(key) for trade in trades for key in trade} - set(default_fields)
    )
    fields = (*default_fields, *extra_fields)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)
    body = buffer.getvalue()
    path = output_dir / f"{prefix}-trades.csv"
    _atomic_write(path, body)
    return ReportArtifact(prefix, path, _sha256(body))


def generate_report_bundle(
    output_dir: Path,
    report_type: ReportType,
    data: dict[str, Any],
    *,
    prefix: str | None = None,
) -> tuple[ReportArtifact, ...]:
    """统一生成 JSON、Markdown、HTML；回测有净值时附 SVG。"""
    validate_report_payload(data)
    name = prefix or f"{report_type}-{data['run_id']}"
    enriched = {
        **data,
        "report_type": report_type,
        "generated_at": datetime.now().astimezone().isoformat(),
    }
    artifacts: list[ReportArtifact] = [
        write_json_report(output_dir, name, enriched),
        write_markdown_report(
            output_dir,
            name,
            {
                "摘要": f"{report_type} / {data['run_id']}",
                "审计数据": "```json\n"
                + json.dumps(enriched, ensure_ascii=False, indent=2, default=str)
                + "\n```",
            },
        ),
        write_html_report(output_dir, name, f"{report_type} 报告", enriched),
    ]
    snapshots = data.get("snapshots")
    if report_type == "backtest" and isinstance(snapshots, list):
        chart = write_equity_svg(output_dir, name, snapshots)
        if chart is not None:
            artifacts.append(chart)
        trades = data.get("trades")
        if isinstance(trades, list) and all(isinstance(item, dict) for item in trades):
            artifacts.append(write_trades_csv(output_dir, name, trades))
    return tuple(artifacts)


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------


@dataclass
class DailyReportData:
    trade_date: date
    run_id: str
    strategy_name: str
    strategy_version: str
    data_version: str
    config_hash: str
    signals: list[dict[str, Any]] = field(default_factory=list)
    target_positions: list[dict[str, Any]] = field(default_factory=list)
    readiness_status: dict[str, Any] = field(default_factory=dict)
    ai_evaluation: dict[str, Any] | None = None


def generate_daily_report(
    output_dir: Path,
    data: DailyReportData,
) -> tuple[ReportArtifact, ReportArtifact]:
    shared = {
        "report_type": "daily",
        "run_id": data.run_id,
        "trade_date": str(data.trade_date),
        "strategy_name": data.strategy_name,
        "strategy_version": data.strategy_version,
        "data_version": data.data_version,
        "config_hash": data.config_hash,
        "generated_at": datetime.now().isoformat(),
        "readiness": data.readiness_status,
        "signals": data.signals,
        "target_positions": data.target_positions,
    }
    if data.ai_evaluation:
        shared["ai_evaluation"] = data.ai_evaluation

    json_artifact = write_json_report(output_dir, str(data.trade_date), shared)

    sections: dict[str, str] = {
        "策略": f"{data.strategy_name} {data.strategy_version}",
        "交易日期": str(data.trade_date),
        "数据版本": data.data_version,
        "信号": "\n".join(
            f"- {s['instrument_id']}: 得分 {s.get('score', 0):.4f}"
            for s in data.signals
        )
        if data.signals
        else "无信号",
    }
    md_artifact = write_markdown_report(output_dir, str(data.trade_date), sections)

    return json_artifact, md_artifact
