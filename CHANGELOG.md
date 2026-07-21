# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Skeleton Phase (2026-07-21)

- Python 3.12 + uv + hatchling project skeleton
- Typed domain models with explicit raw/qfq price boundaries
- Data provider, validation, readiness, storage, pipeline and snapshot contracts
- Explicit multi-strategy registry with independent configuration packages
- Runnable Typer commands for data bootstrap and strategy validation
- Deterministic validation with ruff, mypy strict and CLI smoke checks

### Design Phase (2026-07-21)

- Architecture design: 4-layer system (data / research / AI / reporting)
- Data contract: Bronze/Silver/Gold pipeline with versioned Gold records
- Readiness gates: DAILY_MARKET_READY, FEATURE_READY, ROTATION_READY, CROSS_SECTION_READY, STOCK_BACKTEST_READY
- Strategy system: one package per strategy, explicit registry, independent config and tests
- Execution model: Signal → PendingOrder → Execution state machine
- AI evaluation: optional read-only DeepSeek explanation with 3 dimensions and hard safety boundaries
- Directory structure: lightweight models / data / research / reporting / cli modules
- Operations: Docker scheduling, PostgreSQL persistence, snapshot distribution
- Migration plan from 04-quantitative-trading and 05-stock-arvester

## [0.1.0] — 2026-07-21

### Added

- Initial project documentation
- `docs/architecture.md`
- `docs/data-contract.md`
- `docs/strategy-guide.md`
- `docs/ai-evaluation.md`
- `docs/operations.md`
- `docs/index.md`
- `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`
- `README.md`, `.env.example`, `.gitignore`
