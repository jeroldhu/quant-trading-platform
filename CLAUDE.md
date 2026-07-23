# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Quantitative trading project for A-share market — currently in **design phase**.

Covers four layers:

1. **Data layer** — ETF/index/stock market data ingestion, multi-source cross-validation, snapshot publishing
2. **Research layer** — factor computation, theme rotation, cross-sectional selection, portfolio allocation, execution simulation, event-driven backtesting
3. **AI evaluation layer** — DeepSeek-powered explanation of backtest results, signal quality, and data anomalies (read-only, never modifies signals)
4. **Reporting layer** — daily/weekly/backtest/quality reports with embedded AI evaluation

The project is a unified refactor of two prior repositories:
- `04-quantitative-trading` (quant-theme): research/strategy/backtest system
- `05-stock-arvester` (etf-pipeline): data ingestion and validation pipeline

Full architecture: `docs/index.md`.

## Build, Test, and Development Commands

Use Python 3.12 and `uv`:

```bash
uv sync --extra dev                    # Install package + dev tools
uv run ruff check .                    # Lint
uv run ruff format .                   # Format
uv run mypy src/quant_trading          # Strict type checking
uv run quant research strategies validate  # Validate strategy configs
```

CLI entry point (when implemented):
```bash
uv run quant --help
uv run quant data daily --trade-date 2026-07-21
uv run quant research backtest --start 2024-01-01 --end 2026-07-21
```

## Architecture

### Project Structure

```
src/quant_trading/
├── models/             # Bar, signal, order, trade, position entities
├── data/               # Flat provider/pipeline/validation/readiness/storage modules
├── research/
│   ├── strategies/     # One package per strategy
│   ├── strategy.py     # Shared Strategy protocol and context
│   ├── strategy_registry.py  # Explicit built-in strategy registry
│   ├── universe.py
│   ├── factors.py
│   ├── portfolio.py
│   └── backtest.py     # Execution simulation + metrics until they need splitting
├── reporting/          # Reports + optional read-only AI evaluation
└── cli/                # Typer commands and composition root
```

### Layer Dependency Rules

```
cli → reporting → research → data → models
```

- `models/` depends on nothing else
- `data/` depends on `models/`, NOT `research/`
- `research/` consumes Gold data through read-only `data/` interfaces
- `reporting/` consumes result objects and never changes gates or signals
- `cli/` is the composition root and can assemble all components
- prefer flat modules; split only when multiple implementations actually exist
- strategies are explicitly listed in `research/strategies/__init__.py`; no auto-discovery

### Data Pipeline: Raw → Bronze → Silver → Gold

```
Raw (gzip HTTP responses) → Bronze (normalized fields, deduplicated)
  → Silver (single-source records, conflicts flagged)
    → Gold (multi-source consensus, PASS/PASS_OFFICIAL status)
```

Quality status: `PASS`, `PASS_OFFICIAL`, `PROVISIONAL`, `STALE`, `QUARANTINED`, `UNSUPPORTED`.

Gates:
- `DAILY_READY` — >= 99% dual-source coverage, publishable
- `ROTATION_READY` — 15/15 candidate ETFs + CSI 300 + calendar all Gold
- `HISTORY_READY` — full-universe 61-day qfq Gold history
- `STOCK_BACKTEST_READY` — limit/suspend/ST permissions available

### Backtest Engine

Event-driven with A-share constraints:
- Signal computed at T close, executed at T+1 open (unadjusted price)
- Limit-up blocks buys, limit-down blocks sales
- Suspended stocks excluded
- Lot size: 100 shares, rounded down
- Commission: 0.03% (min 5 CNY), stamp duty: 0.05% sell only (0 for ETFs)
- Slippage: 0.05%
- Point-in-time mode: no future data leakage

### Configuration

Layered: env vars (.env) → YAML files (configs/*.yaml) → Pydantic defaults.

Key env vars: `QUANT_DATA_ROOT`, `QUANT_MARKET_MODE` (`snapshot` / `live`), `QUANT_TUSHARE_TOKEN`, `QUANT_TENCENT_ENABLED`, `DEEPSEEK_API_KEY`.

Config files: `configs/pipeline.yaml`, `configs/universes.yaml`, and one file per
strategy under `configs/strategies/`.

### Key Design Decisions

- **DuckDB over SQLite**. Columnar engine is 10-100x faster for time-series scans in backtesting. Native Parquet zero-copy reads align with the pipeline's Parquet storage.
- **Parquet + ZSTD partitioning**. Monthly partitions with ZSTD compression. Atomic writes via temp-file-then-rename.
- **Multi-source consensus over single-source trust**. No single web source is trusted for formal signals. Minimum 2 independent sources must agree. Same underlying source wrapped differently (AKShare + Eastmoney) counts as ONE source.
- **Snapshot distribution over live queries**. Aliyun is the sole writer. Research machines pull read-only snapshots via SSH/rsync. Never open DuckDB remotely or reverse-sync.
- **Fail-closed gates**. Data quality failures block signal generation — no silent degradation.
- **AI evaluation as read-only enhancement**. DeepSeek evaluations run after strategy completion, consume results via defined interfaces, and never modify signals or data. Evaluation failures don't block the pipeline — reports mark the evaluation as unavailable.

## Coding Style & Naming Conventions

- 4-space indentation, 88-char line limit, Python 3.12 syntax, complete type annotations (mypy strict)
- `snake_case` for modules/functions/variables, `PascalCase` for classes, `UPPER_CASE` for constants
- Run `uv run ruff format .` before committing
- Keep I/O at module boundaries
- Make trading assumptions explicit in docstrings or configuration — never silent defaults for fees, taxes, or prices

## Validation Guidelines

- Validation must be deterministic and offline. Do not call live market sources.
- Run ruff, mypy strict, strategy configuration validation, and relevant CLI smoke commands.
- Changes to execution rules, adjusted prices, data fusion, or point-in-time membership need reproducible fixtures or comparison evidence.

## Security & Data Integrity

Never commit: `.env`, tokens, DuckDB files, generated reports, PostgreSQL data, backups, passwords.

- Copy `.env.example` for local credentials
- Preserve point-in-time data boundaries — don't replace missing market facts with silent defaults
- Quality gates must fail loudly, not silently degrade
- Raw HTTP responses carry SHA-256 hashes for auditability

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
