"""DuckDB 审计数据到 PostgreSQL 的显式、可重复迁移。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from quant_trading.data.storage import ParquetDuckDBStore, PostgresAuditStore

ALLOWED_TABLES = (
    "data_readiness",
    "etl_run",
    "quality_issue",
    "snapshot_audit",
)


@dataclass(frozen=True, slots=True)
class MigrationResult:
    counts: dict[str, int]
    dry_run: bool


def migrate_audit_tables(
    source: ParquetDuckDBStore,
    target: PostgresAuditStore,
    tables: Sequence[str],
    *,
    dry_run: bool,
) -> MigrationResult:
    """迁移白名单表；目标端全部使用主键/唯一键保证幂等。"""
    selected = tuple(dict.fromkeys(tables))
    unknown = sorted(set(selected) - set(ALLOWED_TABLES))
    if unknown:
        raise ValueError(f"不允许迁移的表: {', '.join(unknown)}")
    if not source.catalog_path.is_file():
        raise RuntimeError(f"DuckDB catalog 不存在: {source.catalog_path}")
    rows: dict[str, list[tuple[object, ...]]] = {}
    with source.connect() as source_connection:
        for table in selected:
            rows[table] = source_connection.execute(f"SELECT * FROM {table}").fetchall()
    counts = {table: len(values) for table, values in rows.items()}
    if dry_run:
        return MigrationResult(counts, True)

    target.bootstrap()
    with (
        psycopg.connect(target.dsn) as target_connection,
        target_connection.cursor() as cursor,
    ):
        if values := rows.get("data_readiness"):
            cursor.executemany(
                """
                INSERT INTO quant_data_readiness
                    (gate, trade_date, data_version, state, payload, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (gate, trade_date, data_version) DO UPDATE SET
                    state=excluded.state,
                    payload=excluded.payload,
                    evaluated_at=excluded.evaluated_at
                """,
                [
                    (
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        Jsonb(
                            {
                                "coverage": row[4],
                                "published_count": row[5],
                                "expected_count": row[6],
                                "blocking_issues": json.loads(str(row[7])),
                                "warnings": json.loads(str(row[8])),
                            }
                        ),
                        row[9],
                    )
                    for row in values
                ],
            )
        if values := rows.get("etl_run"):
            cursor.executemany(
                """
                INSERT INTO quant_etl_run VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id) DO UPDATE SET
                    data_version=excluded.data_version,
                    status=excluded.status,
                    finished_at=excluded.finished_at,
                    detail=excluded.detail
                """,
                values,
            )
        if values := rows.get("quality_issue"):
            cursor.executemany(
                """
                INSERT INTO quant_quality_issue VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                [(*row[:7], Jsonb(json.loads(str(row[7]))), row[8]) for row in values],
            )
        if values := rows.get("snapshot_audit"):
            cursor.executemany(
                """
                INSERT INTO quant_snapshot_audit VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (snapshot_id) DO NOTHING
                """,
                values,
            )
        target_connection.commit()
    return MigrationResult(counts, False)
