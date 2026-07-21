# Repository Guidelines

## Project Structure & Module Organization

```
src/quant_trading/
├── models/             # Domain models split by entity
├── data/               # Providers, pipeline, validation, readiness, storage, snapshots
├── research/           # Factors, portfolio, backtest, strategy registry and strategies
├── reporting/          # Reports and optional read-only AI evaluation
└── cli/                # Typer CLI entry points
```

Boundaries:
- `models/` — zero business dependencies
- `data/` — depends on `models/`, NOT `research/`
- `research/` — consumes `data/` through read-only Gold-data interfaces
- `reporting/` — consumes result objects; AI evaluation remains optional and read-only
- `cli/` — composition root and the only layer allowed to assemble all components

Domain models live in `models/`, split by entity (bar.py, signal.py, order.py,
trade.py, position.py). Do not create a single models.py monolith.

Use flat modules first. Split `providers.py`, `storage.py`, `factors.py`, or
`backtest.py` into packages only after they contain multiple independent
implementations or clearly separate responsibilities. Strategies are organized
as one package per strategy under `research/strategies/` and exposed through an
explicit `strategy_registry.py`; do not use filesystem scanning or import side
effects for registration.

## Build, Test, and Development Commands

```bash
uv sync --extra dev                    # Install + dev tools
uv run ruff check .                    # Lint
uv run ruff format .                   # Format
uv run mypy src/quant_trading          # Strict type check
uv run quant research strategies validate  # Validate strategy configs
```

## Coding Style & Naming Conventions

- 4-space indentation, 88-char line limit, Python 3.12 syntax
- Complete type annotations (mypy strict mode)
- `snake_case` for modules/functions/variables, `PascalCase` for classes, `UPPER_CASE` for constants
- Run `uv run ruff format .` before committing
- Keep I/O at module boundaries
- Make trading assumptions explicit in docstrings or config — never silent defaults for fees, taxes, or prices

## Validation Guidelines

- Validation must be deterministic and offline; do not call live market sources
- Run ruff, mypy strict, strategy config validation, and relevant CLI smoke commands
- Changes to execution rules, adjusted prices, data fusion, or point-in-time membership need reproducible fixtures or comparison evidence

## Commit & Pull Request Guidelines

- Conventional commits: `feat(backtest):`, `fix(data):`, `docs(contracts):`, `refactor(models):`
- Breaking changes: `feat(strategy)!:` or `BREAKING CHANGE:` footer
- PRs must pass ruff + mypy + strategy configuration validation before review
- Strategy changes must include before/after backtest comparison in PR body

## Security & Data Integrity

Never commit: `.env`, tokens, DuckDB files, generated reports, PostgreSQL data, backups, passwords.

- Quality gates fail loudly — no silent degradation
- Point-in-time data boundaries must be preserved
- AI evaluation is read-only and never modifies signals, gates, or orders
- Raw HTTP responses carry SHA-256 hashes for auditability

## Branch Strategy

- `main` — protected, reflects production state
- Feature branches: `feat/<description>`, `fix/<description>`
- Strategy experiment branches: `exp/<description>` (may contain non-reproducible notebooks)
