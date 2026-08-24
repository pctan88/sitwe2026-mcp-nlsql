"""FastMCP server exposing the three RC1 primitives.

Resources
---------
* ``schema://current/{database_id}`` — returns canonical fingerprint, compact
  schema-prompt block, and (if a previous fingerprint is cached) a classified
  diff over the five EvoSchema operator types.

Tools
-----
* ``query/relink``   — AST rewrite with LLM-assisted fallback.
* ``query/validate`` — execute candidate SQL and emit a three-state verdict.

Run::

    python -m mcp_server.server --db data/concert_singer_post.sqlite

The server uses STDIO transport by default (the canonical MCP transport for
local self-hosted servers).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

try:
    from fastmcp import FastMCP  # type: ignore
except Exception:  # pragma: no cover - server import is optional in pilot mode
    FastMCP = None  # type: ignore

from . import fingerprint as fp
from . import relink as rl
from . import validate as vl


# --------------------------------------------------------------------------- #
# Server-side state                                                           #
# --------------------------------------------------------------------------- #

class SchemaCache:
    """Last-seen fingerprint per database_id, used to compute session diffs."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._snapshots: dict[str, fp.Schema] = {}

    def get(self, db_id: str) -> tuple[str | None, fp.Schema | None]:
        return self._store.get(db_id), self._snapshots.get(db_id)

    def put(self, db_id: str, schema: fp.Schema) -> None:
        self._store[db_id] = schema.fingerprint()
        self._snapshots[db_id] = schema


_CACHE = SchemaCache()


def _resolve_db(db_id: str, base_dir: Path) -> Path:
    """Resolve a database id to a path under ``base_dir``."""
    p = (base_dir / f"{db_id}.sqlite").resolve()
    if not p.exists():
        raise FileNotFoundError(f"database id {db_id!r} not found at {p}")
    return p


# --------------------------------------------------------------------------- #
# FastMCP wiring                                                              #
# --------------------------------------------------------------------------- #

def build_server(base_dir: Path) -> "FastMCP":
    if FastMCP is None:
        raise RuntimeError(
            "fastmcp is not installed; install with `pip install fastmcp`."
        )

    server = FastMCP(name="schema-aware-nl2sql")

    @server.resource("schema://current/{database_id}")
    def schema_resource(database_id: str) -> str:
        """Return canonical fingerprint, compact schema, and diff vs last session."""
        path = _resolve_db(database_id, base_dir)
        cur_schema = fp.introspect(str(path))
        prev_fp, prev_schema = _CACHE.get(database_id)
        diff = (
            fp.classify(prev_schema, cur_schema)
            if prev_schema is not None
            else fp.SchemaDiff(
                pre_fingerprint="", post_fingerprint=cur_schema.fingerprint()
            )
        )
        _CACHE.put(database_id, cur_schema)
        payload = {
            "database_id": database_id,
            "fingerprint": cur_schema.fingerprint(),
            "previous_fingerprint": prev_fp,
            "diff": diff.to_dict(),
            "schema": fp.to_prompt_block(cur_schema),
        }
        return json.dumps(payload, indent=2)

    @server.tool(name="query/relink")
    def query_relink(
        stale_sql: str,
        diff_events: list[dict[str, Any]],
        schema_text: str | None = None,
    ) -> dict[str, Any]:
        """Rewrite stale SQL against the evolved schema."""
        return rl.relink(stale_sql, diff_events, schema_text=schema_text)

    @server.tool(name="query/validate")
    def query_validate(
        database_id: str,
        sql: str,
        original_question: str,
    ) -> dict[str, Any]:
        """Execute SQL and emit a three-state verdict."""
        path = _resolve_db(database_id, base_dir)
        result = vl.validate(str(path), sql, original_question)
        return result.to_dict()

    return server


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Schema-aware MCP server (RC1).")
    p.add_argument(
        "--db-dir",
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="Directory containing <database_id>.sqlite files.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    base = Path(args.db_dir).resolve()
    server = build_server(base)
    # Default transport is STDIO; transports can be overridden via fastmcp CLI.
    server.run()


if __name__ == "__main__":
    main()
