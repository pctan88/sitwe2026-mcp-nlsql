"""Per-database configuration loading for pilot runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DatabaseConfig:
    database_id: str
    root: Path
    pre_db: Path
    post_db: Path
    queries_path: Path
    perturbation_manifest: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, database: str, data_root: Path) -> "DatabaseConfig":
        cfg_path = (data_root / database / "config.json").resolve()
        if not cfg_path.exists():
            raise FileNotFoundError(f"database config not found: {cfg_path}")
        with cfg_path.open() as f:
            raw = json.load(f)
        root = cfg_path.parent
        return cls(
            database_id=raw.get("database_id", database),
            root=root,
            pre_db=(root / raw["pre_db"]).resolve(),
            post_db=(root / raw["post_db"]).resolve(),
            queries_path=(root / raw["queries"]).resolve(),
            perturbation_manifest=raw.get("perturbation_manifest", {}),
        )

    @classmethod
    def from_legacy_paths(
        cls,
        database_id: str,
        db_dir: Path,
        queries_path: Path,
    ) -> "DatabaseConfig":
        return cls(
            database_id=database_id,
            root=db_dir.resolve(),
            pre_db=(db_dir / "concert_singer_pre.sqlite").resolve(),
            post_db=(db_dir / "concert_singer_post.sqlite").resolve(),
            queries_path=queries_path.resolve(),
            perturbation_manifest={},
        )


def list_database_configs(data_root: Path) -> list[DatabaseConfig]:
    configs: list[DatabaseConfig] = []
    for cfg_path in sorted(data_root.glob("*/config.json")):
        configs.append(DatabaseConfig.load(cfg_path.parent.name, data_root))
    return configs
