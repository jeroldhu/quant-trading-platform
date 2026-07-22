"""可校验数据快照的创建、拉取、验证与原子恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from quant_trading.data.storage import ParquetDuckDBStore, manifest_sha256

SnapshotProfile = Literal["dev", "full"]


class SnapshotFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    snapshot_id: str
    profile: SnapshotProfile
    created_at: datetime
    data_version: str
    host: str
    git_commit: str
    duckdb_checkpoint: str
    files: tuple[SnapshotFile, ...]


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _copy_file(source: Path, target: Path) -> None:
    """复制快照文件，禁止硬链接生产数据的可变 inode。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _checkpoint(catalog_path: Path) -> str:
    if not catalog_path.is_file():
        raise RuntimeError(f"DuckDB catalog 不存在: {catalog_path}")
    with duckdb.connect(str(catalog_path)) as connection:
        connection.execute("CHECKPOINT")
    stat = catalog_path.stat()
    return f"{stat.st_size}-{stat.st_mtime_ns}"


def _included_files(data_root: Path, profile: SnapshotProfile) -> tuple[Path, ...]:
    allowed = {"bronze", "silver", "gold", "catalog"}
    if profile == "full":
        allowed.add("raw")
    files = tuple(
        sorted(
            path
            for child in allowed
            for path in (data_root / child).rglob("*")
            if path.is_file() and not path.name.endswith(".wal")
        )
    )
    if not files:
        raise RuntimeError("数据目录没有可快照文件，拒绝创建空快照")
    if not any(path.suffix == ".duckdb" for path in files):
        raise RuntimeError("快照缺少 DuckDB catalog")
    if not any(path.suffix == ".parquet" for path in files):
        raise RuntimeError("快照缺少 Parquet 数据")
    return files


def _atomic_latest(snapshot_root: Path, profile: SnapshotProfile, target: Path) -> None:
    link = snapshot_root / f"latest-{profile}"
    temporary = snapshot_root / f".latest-{profile}-{uuid4().hex}"
    temporary.symlink_to(target.name, target_is_directory=True)
    os.replace(temporary, link)


def create_snapshot(
    data_root: Path,
    snapshot_root: Path,
    data_version: str,
    *,
    profile: SnapshotProfile = "dev",
    retain: int = 7,
) -> SnapshotManifest:
    """创建一致快照；校验全部通过后才原子更新 ``latest-*``。"""

    data_root = data_root.resolve()
    snapshot_root = snapshot_root.resolve()
    snapshot_root.mkdir(parents=True, exist_ok=True)
    # manifest 只能声明已经完成发布的逻辑版本，防止快照内容与版本标签脱节。
    ParquetDuckDBStore(data_root).version_chain(data_version)
    checkpoint = _checkpoint(data_root / "catalog" / "quant.duckdb")
    snapshot_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + f"-{uuid4().hex[:8]}"
    staging = snapshot_root / f".{snapshot_id}.staging"
    final = snapshot_root / snapshot_id
    if staging.exists() or final.exists():
        raise FileExistsError(snapshot_id)
    staging_data = staging / "data"
    staging_data.mkdir(parents=True)
    try:
        files: list[SnapshotFile] = []
        for source in _included_files(data_root, profile):
            relative = source.relative_to(data_root)
            target = staging_data / relative
            _copy_file(source, target)
            files.append(
                SnapshotFile(
                    path=f"data/{relative.as_posix()}",
                    sha256=manifest_sha256(target),
                    size=target.stat().st_size,
                )
            )
        manifest = SnapshotManifest(
            snapshot_id=snapshot_id,
            profile=profile,
            created_at=datetime.now(UTC),
            data_version=data_version,
            host=socket.gethostname(),
            git_commit=_git_commit(),
            duckdb_checkpoint=checkpoint,
            files=tuple(files),
        )
        manifest_path = staging / "manifest.json"
        content = manifest.model_dump_json(indent=2).encode()
        manifest_path.write_bytes(content)
        (staging / "manifest.sha256").write_text(
            _sha256_bytes(content) + "\n", encoding="ascii"
        )
        verify_snapshot(staging)
        os.replace(staging, final)
        _atomic_latest(snapshot_root, profile, final)
        prune_snapshots(snapshot_root, profile, retain)
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def read_manifest(snapshot_dir: Path) -> SnapshotManifest:
    manifest_path = snapshot_dir / "manifest.json"
    hash_path = snapshot_dir / "manifest.sha256"
    if not manifest_path.is_file() or not hash_path.is_file():
        raise RuntimeError("快照缺少 manifest.json 或 manifest.sha256")
    content = manifest_path.read_bytes()
    expected = hash_path.read_text(encoding="ascii").strip()
    actual = _sha256_bytes(content)
    if actual != expected:
        raise RuntimeError("manifest.sha256 不匹配")
    return SnapshotManifest.model_validate_json(content)


def verify_snapshot(snapshot_dir: Path) -> dict[str, object]:
    """逐文件验证快照；任一缺失、尺寸或哈希错误都失败。"""

    snapshot_dir = snapshot_dir.resolve()
    manifest = read_manifest(snapshot_dir)
    failures: list[str] = []
    for item in manifest.files:
        target = (snapshot_dir / item.path).resolve()
        try:
            target.relative_to(snapshot_dir)
        except ValueError:
            failures.append(f"UNSAFE_PATH:{item.path}")
            continue
        if not target.is_file():
            failures.append(f"MISSING:{item.path}")
            continue
        if target.stat().st_size != item.size:
            failures.append(f"SIZE_MISMATCH:{item.path}")
            continue
        if manifest_sha256(target) != item.sha256:
            failures.append(f"HASH_MISMATCH:{item.path}")
    if not manifest.files:
        failures.append("EMPTY_MANIFEST")
    catalog = snapshot_dir / "data" / "catalog" / "quant.duckdb"
    if catalog.is_file():
        try:
            with duckdb.connect(str(catalog), read_only=True) as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception as exc:
            failures.append(f"DUCKDB_INVALID:{exc}")
    else:
        failures.append("DUCKDB_MISSING")
    result: dict[str, object] = {
        "snapshot_id": manifest.snapshot_id,
        "profile": manifest.profile,
        "valid": not failures,
        "files_total": len(manifest.files),
        "failures": failures,
    }
    if failures:
        raise RuntimeError(
            f"快照 {manifest.snapshot_id} 校验失败: {'; '.join(failures)}"
        )
    return result


def prune_snapshots(
    snapshot_root: Path, profile: SnapshotProfile, retain: int
) -> tuple[str, ...]:
    if retain < 1:
        raise ValueError("retain 必须 >= 1")
    candidates: list[tuple[datetime, Path]] = []
    for child in snapshot_root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            manifest = read_manifest(child)
        except Exception:
            continue
        if manifest.profile == profile:
            candidates.append((manifest.created_at, child))
    removed: list[str] = []
    for _, path in sorted(candidates, reverse=True)[retain:]:
        shutil.rmtree(path)
        removed.append(path.name)
    return tuple(removed)


def restore_snapshot(
    snapshot_dir: Path,
    data_root: Path,
    *,
    backup_existing: bool = False,
) -> SnapshotManifest:
    """验证后原子恢复；未知非空目录默认拒绝覆盖。"""

    verify_snapshot(snapshot_dir)
    manifest = read_manifest(snapshot_dir)
    source = snapshot_dir.resolve() / "data"
    target = data_root.resolve()
    origin_path = target / ".snapshot-origin.json"
    if target.exists() and any(target.iterdir()):
        if origin_path.is_file():
            origin = json.loads(origin_path.read_text(encoding="utf-8"))
            if origin.get("snapshot_id") == manifest.snapshot_id:
                return manifest
        if not backup_existing:
            raise RuntimeError(
                f"目标目录非空且来源不可验证: {target}；"
                "如需替换请显式使用 --backup-existing"
            )

    staging = target.parent / f".{target.name}.restore-{uuid4().hex}"
    shutil.copytree(source, staging)
    (staging / ".snapshot-origin.json").write_text(
        json.dumps(
            {
                "snapshot_id": manifest.snapshot_id,
                "manifest_sha256": _sha256_bytes(
                    (snapshot_dir / "manifest.json").read_bytes()
                ),
                "restored_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    previous: Path | None = None
    try:
        if target.exists():
            previous = target.parent / (
                f"{target.name}.backup-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
            )
            os.replace(target, previous)
        os.replace(staging, target)
        ParquetDuckDBStore(target).refresh_views()
    except Exception:
        if not target.exists() and previous is not None and previous.exists():
            os.replace(previous, target)
        raise
    return manifest


def pull_snapshot(
    *,
    remote: str,
    remote_snapshot_root: str,
    local_snapshot_root: Path,
    data_root: Path,
    profile: SnapshotProfile = "dev",
    snapshot_id: str | None = None,
    backup_existing: bool = False,
) -> SnapshotManifest:
    """通过 rsync 拉取只读快照，校验后恢复到本地。"""

    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", remote):
        raise ValueError("remote 只允许 SSH 别名、主机名或 user@host")
    if not remote_snapshot_root.startswith("/"):
        raise ValueError("remote_snapshot_root 必须是绝对路径")
    selector = snapshot_id or f"latest-{profile}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", selector):
        raise ValueError("非法 snapshot_id")
    local_snapshot_root.mkdir(parents=True, exist_ok=True)
    staging = local_snapshot_root / f".{selector}.pull-{uuid4().hex}"
    staging.mkdir()
    remote_path = f"{remote}:{remote_snapshot_root.rstrip('/')}/{selector}/"
    result = subprocess.run(
        ["rsync", "-a", "--partial", "--safe-links", remote_path, f"{staging}/"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"rsync 拉取失败: {result.stderr.strip()}")
    manifest = read_manifest(staging)
    verify_snapshot(staging)
    final = local_snapshot_root / manifest.snapshot_id
    if final.exists():
        verify_snapshot(final)
        shutil.rmtree(staging)
    else:
        os.replace(staging, final)
    _atomic_latest(local_snapshot_root, manifest.profile, final)
    return restore_snapshot(final, data_root, backup_existing=backup_existing)


def verify_data_root(data_root: Path) -> dict[str, object]:
    origin = data_root / ".snapshot-origin.json"
    if not origin.is_file():
        raise RuntimeError("本地数据目录没有可验证的快照来源")
    metadata = json.loads(origin.read_text(encoding="utf-8"))
    catalog = data_root / "catalog" / "quant.duckdb"
    if not catalog.is_file():
        raise RuntimeError("本地数据目录缺少 DuckDB catalog")
    with duckdb.connect(str(catalog), read_only=True) as connection:
        connection.execute("SELECT 1").fetchone()
    return {
        "valid": True,
        "snapshot_id": metadata.get("snapshot_id"),
        "catalog": str(catalog),
    }
