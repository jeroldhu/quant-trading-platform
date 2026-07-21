# Quant Trading — A 股量化研究系统

A 股主题轮动、ETF 横截面多因子与事件驱动回测的统一量化研究平台。

> **当前阶段：设计阶段。** 仓库包含完整架构设计和文档，`src/`、`tests/`、`configs/`
> 代码骨架待实现。以下命令为规划接口，将在 Phase 2~4 逐步可用。

本项目是 `04-quantitative-trading` 和 `05-stock-arvester` 的重构合并产物，
覆盖数据采集、多源验证、因子计算、策略回测、AI 评估和报告输出的完整链路。

**不连接券商**，所有输出仅用于量化研究和策略验证，不构成投资建议。

## 分层架构

```
models/                 data/                 research/              reporting/
──────────────         ──────────────        ─────────────────     ──────────────
bar/signal/order       providers            universe/factors      reports
trade/position         pipeline             portfolio/backtest    ai_evaluation
                       validation
                       readiness            strategies/
                       storage/snapshot     strategy_registry

cli/                   Typer 命令入口
```

骨架遵循“先模块、后包化”：只有出现多个独立实现或文件职责明显分裂时才继续拆包。
策略是例外——从第一阶段起采用“一策略一包 + 显式注册表”，方便独立增删、配置和测试。

完整设计：`docs/index.md`

## 环境

- Python 3.12+
- uv
- Asia/Shanghai 时区

```bash
uv sync --extra dev
cp .env.example .env
```

Python 虚拟环境统一使用 `.venv/`；`.env` 必须是环境变量文件，不能作为虚拟环境目录。

## 快速开始（规划接口）

```bash
# 查看所有命令
uv run quant --help

# 数据管道（生产服务器）
uv run quant data bootstrap
uv run quant data daily --trade-date 2026-07-21

# 快照分发（研究端拉取）
uv run quant data snapshot pull --remote aliyun --profile dev

# 研究回测
uv run quant research daily-run --trade-date 2026-07-21
uv run quant research backtest --start 2024-01-01 --end 2026-07-21
```

## 验证

```bash
uv run ruff check .
uv run mypy src/quant_trading
uv run pytest
```

## 文档

| 文档 | 内容 | 完成度 |
|------|------|--------|
| `docs/index.md` | 文档导航 | ✅ |
| `docs/architecture.md` | 架构、边界、迁移计划 | ✅ |
| `docs/data-contract.md` | 数据集、门禁和快照契约 | ✅ |
| `docs/strategy-guide.md` | 策略增删、配置和回测指南 | ✅ |
| `docs/ai-evaluation.md` | AI 评估配置与安全边界 | ✅ |
| `docs/operations.md` | 部署、调度与恢复 | ✅ |
| `CLAUDE.md` | AI 编码助手指南 | ✅ |
| `AGENTS.md` | 仓库规范 | ✅ |

## 旧项目

- `04-quantitative-trading` — 研究/策略/回测系统（重构后将归档）
- `05-stock-arvester` — 数据采集管道（重构后将归档）

## License

MIT
