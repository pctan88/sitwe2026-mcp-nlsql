"""Build per-operator fixture folders for each database."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.perturbations import (
    PerturbationOperator,
    PerturbationSpec,
    apply_perturbations,
)

DATA_ROOT = Path(__file__).resolve().parent

BASE_DBS = {
    "concert_singer": DATA_ROOT / "concert_singer" / "concert_singer_pre.sqlite",
    "hr_1": DATA_ROOT / "hr_1" / "hr_1_pre.sqlite",
}

FIXTURES: dict[str, dict[str, list[PerturbationSpec]]] = {
    "concert_singer": {
        "TABLE_RENAME": [
            PerturbationSpec(
                PerturbationOperator.TABLE_RENAME,
                {"from": "singer", "to": "artist"},
            ),
        ],
        "TABLE_SPLIT": [
            PerturbationSpec(
                PerturbationOperator.TABLE_SPLIT,
                {
                    "source": "singer",
                    "targets": ["singer_info", "singer_songs"],
                    "key": "Singer_ID",
                    "partition": {
                        "singer_info": ["Name", "Country", "Age", "Is_male"],
                        "singer_songs": ["Song_Name", "Song_release_year"],
                    },
                },
            ),
        ],
        "TABLE_MERGE": [
            PerturbationSpec(
                PerturbationOperator.TABLE_MERGE,
                {
                    "sources": ["concert", "singer_in_concert"],
                    "target": "concert_performance",
                    "join_key": "Concert_ID",
                },
            ),
        ],
        "COLUMN_RENAME": [
            PerturbationSpec(
                PerturbationOperator.COLUMN_RENAME,
                {"table": "singer", "from": "Song_Name", "to": "song_title"},
            ),
            PerturbationSpec(
                PerturbationOperator.COLUMN_RENAME,
                {"table": "singer", "from": "Song_release_year", "to": "debut_year"},
            ),
        ],
        "COLUMN_MERGE": [
            PerturbationSpec(
                PerturbationOperator.COLUMN_MERGE,
                {
                    "table": "singer",
                    "sources": ["Name", "Country"],
                    "target": "Name_Country",
                    "expression": "Name || ' - ' || Country",
                    "target_type": "TEXT",
                    "reversible": False,
                },
            ),
        ],
    },
    "hr_1": {
        "TABLE_RENAME": [
            PerturbationSpec(
                PerturbationOperator.TABLE_RENAME,
                {"from": "employees", "to": "staff"},
            ),
        ],
        "TABLE_SPLIT": [
            PerturbationSpec(
                PerturbationOperator.TABLE_SPLIT,
                {
                    "source": "employees",
                    "targets": ["employee_personal", "employee_job"],
                    "key": "employee_id",
                    "partition": {
                        "employee_personal": ["first_name", "last_name", "email", "phone_number"],
                        "employee_job": [
                            "hire_date",
                            "job_id",
                            "salary",
                            "commission_pct",
                            "manager_id",
                            "department_id",
                        ],
                    },
                },
            ),
        ],
        "TABLE_MERGE": [
            PerturbationSpec(
                PerturbationOperator.TABLE_MERGE,
                {
                    "sources": ["departments", "locations"],
                    "target": "department_location",
                    "join_key": "location_id",
                },
            ),
        ],
        "COLUMN_RENAME": [
            PerturbationSpec(
                PerturbationOperator.COLUMN_RENAME,
                {"table": "employees", "from": "salary", "to": "monthly_salary"},
            ),
            PerturbationSpec(
                PerturbationOperator.COLUMN_RENAME,
                {"table": "employees", "from": "first_name", "to": "fname"},
            ),
        ],
        "COLUMN_MERGE": [
            PerturbationSpec(
                PerturbationOperator.COLUMN_MERGE,
                {
                    "table": "employees",
                    "sources": ["first_name", "last_name"],
                    "target": "full_name",
                    "expression": "first_name || ' ' || last_name",
                    "target_type": "TEXT",
                    "reversible": False,
                },
            ),
        ],
    },
}


def build_fixture(
    database_id: str,
    op_name: str,
    pre_db_path: Path,
    specs: list[PerturbationSpec],
) -> None:
    fixture_dir = DATA_ROOT / f"{database_id}_{op_name}"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    pre_name = f"{database_id}_pre.sqlite"
    post_name = f"{database_id}_post.sqlite"
    pre_dst = fixture_dir / pre_name
    post_dst = fixture_dir / post_name

    shutil.copyfile(pre_db_path, pre_dst)
    manifest = apply_perturbations(str(pre_dst), str(post_dst), specs)

    config = {
        "database_id": f"{database_id}_{op_name}",
        "pre_db": pre_name,
        "post_db": post_name,
        "queries": "queries.json",
        "perturbation_manifest": manifest.to_dict(),
    }

    (fixture_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (fixture_dir / "queries.json").write_text("[]\n")


def main() -> None:
    missing = [str(p) for p in BASE_DBS.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing base pre-DBs. Run data/build_dbs.py first: " + ", ".join(missing)
        )

    for database_id, ops in FIXTURES.items():
        pre_db_path = BASE_DBS[database_id]
        for op_name, specs in ops.items():
            build_fixture(database_id, op_name, pre_db_path, specs)


if __name__ == "__main__":
    main()
