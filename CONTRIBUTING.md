# Contributing

## Development Setup

```bash
git clone <repo-url>
cd quant-trading
uv sync --extra dev
cp .env.example .env
uv run quant research strategies validate
```

## Making Changes

1. Create a feature branch from `main`
2. Make changes, add tests
3. Run `uv run ruff check . && uv run mypy src/quant_trading`
4. Run `uv run quant research strategies validate`
4. For strategy changes: run backtest before/after and include comparison in PR
5. For data contract changes: update `docs/contracts/` and bump schema versions
6. Open PR with conventional commit title

## What Needs What

| Change | Required Checks |
|--------|----------------|
| New data source | Provider adapter + validation tests + `docs/contracts/datasets.md` |
| New factor | Factor unit tests + factor exposure check |
| New strategy | Strategy integration tests + backtest comparison |
| Config change | `quant config validate` + backtest before/after |
| Schema change | Schema version bump + migration script + backward compat plan |
| AI prompt change | Dry-run with historical results + JSON Schema validation |

## Document First

For significant features, write or update the relevant design document before coding:
- Data pipeline changes → `docs/contracts/`
- Strategy changes → `docs/architecture/` + `docs/guides/strategy-development.md`
- Deployment changes → `docs/runbooks/`

## Review Checklist

- [ ] Ruff, mypy and strategy configuration validation pass
- [ ] No silent defaults for fees, taxes, prices
- [ ] Point-in-time safety preserved
- [ ] Quality gates fail loudly
- [ ] Relevant docs updated
- [ ] Conventional commit message
