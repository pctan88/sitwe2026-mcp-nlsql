"""Verify per-operator fixtures for integrity and manifest metadata."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent

EXPECTED_FIXTURES = [
    "concert_singer_TABLE_RENAME",
    "concert_singer_TABLE_SPLIT",
    "concert_singer_TABLE_MERGE",
    "concert_singer_COLUMN_RENAME",
    "concert_singer_COLUMN_MERGE",
    "hr_1_TABLE_RENAME",
    "hr_1_TABLE_SPLIT",
    "hr_1_TABLE_MERGE",
    "hr_1_COLUMN_RENAME",
    "hr_1_COLUMN_MERGE",
]

HEX_CHARS = set("0123456789abcdef")


def _check_manifest(manifest: dict) -> None:
    engine_version = manifest.get("engine_version")
    content_hash = manifest.get("content_hash")
    if not engine_version:
        raise AssertionError("manifest missing engine_version")
    if not content_hash or len(content_hash) != 16:
        raise AssertionError("manifest content_hash must be 16 hex chars")
    if not all(c in HEX_CHARS for c in content_hash):
        raise AssertionError("manifest content_hash is not hex")


def _check_integrity(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
    finally:
        con.close()
    if not result or result[0] != "ok":
        raise AssertionError(f"integrity_check failed for {db_path}: {result}")


def main() -> None:
    missing = [name for name in EXPECTED_FIXTURES if not (DATA_ROOT / name).exists()]
    if missing:
        raise FileNotFoundError("Missing fixture directories: " + ", ".join(missing))

    for name in EXPECTED_FIXTURES:
        root = DATA_ROOT / name
        cfg_path = root / "config.json"
        if not cfg_path.exists():
            raise FileNotFoundError(f"Missing config.json in {root}")
        config = json.loads(cfg_path.read_text())
        manifest = config.get("perturbation_manifest")
        if not isinstance(manifest, dict):
            raise AssertionError(f"Missing perturbation_manifest in {cfg_path}")
        _check_manifest(manifest)

        post_db = root / config["post_db"]
        _check_integrity(post_db)

    print(f"[ok] verified {len(EXPECTED_FIXTURES)} fixtures")


if __name__ == "__main__":
    main()

