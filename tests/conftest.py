"""Shared pytest fixtures for SITWE 2026 tests.

Provides temporary pre/post SQLite databases built from the canonical
concert_singer fixture (data/build_dbs.py) and corresponding Schema objects.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from data.build_dbs import build_pre, build_post


@pytest.fixture(autouse=True)
def _block_network_if_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("BLOCK_NETWORK") != "1":
        return

    def _blocked(*args, **kwargs):
        raise RuntimeError("Network access blocked for tests")

    monkeypatch.setattr("socket.create_connection", _blocked)


# --------------------------------------------------------------------------- #
# Database fixtures                                                           #
# --------------------------------------------------------------------------- #

@pytest.fixture
def pre_db(tmp_path: Path) -> str:
    """Create a concert_singer pre-perturbation SQLite database.

    Returns the path as a string (the format expected by all MCP modules).
    """
    db_path = tmp_path / "concert_singer_pre.sqlite"
    build_pre(db_path)
    return str(db_path)


@pytest.fixture
def post_db(tmp_path: Path) -> str:
    """Create a concert_singer post-perturbation database (with renames)."""
    db_path = tmp_path / "concert_singer_post.sqlite"
    build_post(db_path)
    return str(db_path)


# --------------------------------------------------------------------------- #
# Schema fixtures                                                             #
# --------------------------------------------------------------------------- #

@pytest.fixture
def pre_schema(pre_db: str):
    """Return a Schema object for the pre-perturbation database."""
    from mcp_server.fingerprint import introspect
    return introspect(pre_db)


@pytest.fixture
def post_schema(post_db: str):
    """Return a Schema object for the post-perturbation database."""
    from mcp_server.fingerprint import introspect
    return introspect(post_db)


# --------------------------------------------------------------------------- #
# Synthetic schema helpers                                                    #
# --------------------------------------------------------------------------- #

def make_simple_db(path: Path, ddl_statements: list[str],
                   inserts: list[tuple[str, list[tuple]]] | None = None) -> str:
    """Create a SQLite database from DDL + optional inserts.

    ``inserts`` is a list of ``(sql_template, rows)`` pairs.
    Returns the path as a string.
    """
    con = sqlite3.connect(str(path))
    try:
        for ddl in ddl_statements:
            con.execute(ddl)
        if inserts:
            for sql, rows in inserts:
                con.executemany(sql, rows)
        con.commit()
    finally:
        con.close()
    return str(path)
