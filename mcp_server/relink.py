"""query/relink Tool — rewrite stale SQL against an evolved schema.

For the five EvoSchema operators recognised by RC1 (column rename, column merge,
table rename, table split, table merge), AST-first rewriting is performed with
``sqlglot``. ``TABLE_SPLIT`` and ``TABLE_MERGE`` fall back to an LLM (left as a
``relink_with_llm`` hook injected by the pilot harness).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

import sqlglot
from sqlglot import exp


# A pluggable LLM fallback: takes (stale_sql, diff_events, schema_text)
# and returns the rewritten SQL string.
LLMFallback = Callable[[str, list[dict[str, Any]], str], str]


def _rename_table_in_ast(tree: exp.Expression, old: str, new: str) -> exp.Expression:
    """Rewrite every Table node whose name equals ``old`` (case-insensitive)."""
    for node in tree.find_all(exp.Table):
        if node.name and node.name.lower() == old.lower():
            node.set("this", exp.to_identifier(new, quoted=False))
    return tree


def _build_alias_map(tree: exp.Expression) -> dict[str, str]:
    """Return a ``{alias_lower -> table_name_lower}`` map for the query tree.

    Spider-style queries routinely alias joined tables (``JOIN artist AS T2``);
    the column-rename pass must resolve ``T2.foo`` back to ``artist.foo``
    before deciding whether to rewrite ``foo``.
    """
    aliases: dict[str, str] = {}
    for node in tree.find_all(exp.Table):
        if not node.name:
            continue
        # The alias may sit on the Table node directly or on an enclosing
        # TableAlias / Alias expression.
        alias_obj = node.args.get("alias")
        alias_name = ""
        if alias_obj is not None:
            alias_name = getattr(alias_obj, "name", "") or ""
            if not alias_name and hasattr(alias_obj, "this") and alias_obj.this:
                alias_name = getattr(alias_obj.this, "name", "") or ""
        if alias_name:
            aliases[alias_name.lower()] = node.name.lower()
        # Tables can also be referenced by their own (unaliased) name.
        aliases.setdefault(node.name.lower(), node.name.lower())
    return aliases


def _rename_column_in_ast(
    tree: exp.Expression,
    table_hint: str,
    old: str,
    new: str,
    alias_map: Optional[dict[str, str]] = None,
) -> exp.Expression:
    """Rewrite every Column node whose name equals ``old``.

    ``table_hint`` is the *post-rename* table name. A column is rewritten when
    (i) it has no qualifier, OR (ii) its qualifier directly equals
    ``table_hint``, OR (iii) its qualifier is an alias that resolves to
    ``table_hint`` via ``alias_map``. Without rule (iii) the rewriter misses
    every Spider-style ``T2.column`` reference — see SHARED_NOTES 2026-05-22.
    """
    am = alias_map or {}
    hint = table_hint.lower()
    for node in tree.find_all(exp.Column):
        if not node.name or node.name.lower() != old.lower():
            continue
        qual = (node.table or "").lower()
        if (not qual) or qual == hint or am.get(qual) == hint:
            node.set("this", exp.to_identifier(new, quoted=False))
    return tree


def build_llm_guidance(diff_events: list[dict[str, Any]]) -> str:
    """Deterministic per-operator rewrite guidance for the LLM fallback prompt.

    The generic relink prompt (stale SQL + raw diff JSON + schema) leaves the
    LLM to infer *how* a merge or split changes filter semantics — the main
    reason COLUMN_MERGE recovery sat at RR 0.00 in the pilot. This function
    turns each complex diff event into explicit, mechanical rewrite rules
    (e.g. equality filters on a merged source column must become LIKE
    patterns over the merge expression's separator).
    """
    lines: list[str] = []
    for ev in diff_events:
        op = ev.get("op")
        if op == "COLUMN_MERGE":
            table = ev.get("table", "?")
            sources = ev.get("sources", [])
            target = ev.get("target", "?")
            expr = ev.get("expression", "")
            src_list = ", ".join(sources)
            lines.append(
                f"COLUMN_MERGE on table {table}: columns [{src_list}] no longer "
                f"exist; they were merged into {target}"
                + (f" via the expression: {expr}." if expr else ".")
            )
            lines.append(
                f"- Any SELECT of [{src_list}] must select {target} instead."
            )
            if expr and "||" in expr:
                # Concatenation merge — derive the literal separator so
                # equality filters can be rewritten as LIKE patterns.
                seps = re.findall(r"\|\|\s*'([^']*)'\s*\|\|", expr)
                sep = seps[0] if seps else " "
                first_src = sources[0] if sources else "?"
                last_src = sources[-1] if sources else "?"
                lines.append(
                    f"- {target} stores values as "
                    f"'<{first_src}>{sep}<{last_src}>'. An equality filter on a "
                    f"source column MUST become a LIKE pattern on {target}: "
                    f"WHERE {first_src} = 'X'  ->  WHERE {target} LIKE 'X{sep}%'; "
                    f"WHERE {last_src} = 'Y'  ->  WHERE {target} LIKE '%{sep}Y'."
                )
                lines.append(
                    f"- To recover an individual component, split on the "
                    f"separator '{sep}' with SUBSTR/INSTR, e.g. "
                    f"SUBSTR({target}, 1, INSTR({target}, '{sep}') - 1) for "
                    f"{first_src}."
                )
            lines.append(
                f"- GROUP BY / ORDER BY / DISTINCT on a source column should "
                f"use the corresponding {target} component (or {target} itself "
                f"when exact component recovery is not required)."
            )
        elif op == "TABLE_SPLIT":
            source = ev.get("source", "?")
            targets = ev.get("targets", [])
            key = ev.get("key", "?")
            tgt_list = ", ".join(targets)
            lines.append(
                f"TABLE_SPLIT: table {source} no longer exists; its columns "
                f"are distributed across [{tgt_list}], sharing the key column "
                f"{key}."
            )
            lines.append(
                f"- Replace FROM {source} with a JOIN of the target tables ON "
                f"their shared {key} column (only join the tables whose "
                f"columns the query actually uses; check the current schema "
                f"for which target table holds each column)."
            )
        elif op == "TABLE_MERGE":
            sources = ev.get("sources", [])
            target = ev.get("target", "?")
            key = ev.get("key", "")
            src_list = ", ".join(sources)
            lines.append(
                f"TABLE_MERGE: tables [{src_list}] no longer exist; their "
                f"columns now live in {target}"
                + (f" (merged on key {key})." if key else ".")
            )
            lines.append(
                f"- Replace references to [{src_list}] with {target} and DROP "
                f"the join between them (the rows are already joined); keep "
                f"any remaining joins to unrelated tables."
            )
    return "\n".join(lines)


def relink(
    stale_sql: str,
    diff_events: list[dict[str, Any]],
    schema_text: Optional[str] = None,
    llm_fallback: Optional[LLMFallback] = None,
    dialect: str = "sqlite",
) -> dict[str, Any]:
    """Return ``{"sql": <rewritten>, "method": "ast"|"llm"|"noop", "applied": [...]}``."""
    if not diff_events:
        return {"sql": stale_sql, "method": "noop", "applied": []}

    # Build alias map: table_rename_map[lower(old)] = new
    table_rename_map: dict[str, str] = {}
    column_renames: list[dict[str, Any]] = []
    has_complex = False

    for ev in diff_events:
        op = ev.get("op")
        if op == "TABLE_RENAME":
            table_rename_map[ev["from"].lower()] = ev["to"]
        elif op == "COLUMN_RENAME":
            column_renames.append(ev)
        elif op in ("TABLE_SPLIT", "TABLE_MERGE", "COLUMN_MERGE"):
            has_complex = True

    # Try AST-first.
    try:
        tree = sqlglot.parse_one(stale_sql, read=dialect)
    except Exception:
        if llm_fallback is not None and schema_text is not None:
            return {
                "sql": llm_fallback(stale_sql, diff_events, schema_text),
                "method": "llm",
                "applied": diff_events,
            }
        return {"sql": stale_sql, "method": "parse_error", "applied": []}

    applied: list[dict[str, Any]] = []
    for old, new in table_rename_map.items():
        tree = _rename_table_in_ast(tree, old, new)
        applied.append({"op": "TABLE_RENAME", "from": old, "to": new})

    # Build alias map AFTER the table rename so aliases now resolve to the
    # post-rename table names (which is what column_renames carries in
    # ``ev["table"]``).
    alias_map = _build_alias_map(tree)

    for ev in column_renames:
        tbl_hint = ev.get("table", "")
        tree = _rename_column_in_ast(
            tree, tbl_hint, ev["from"], ev["to"], alias_map=alias_map
        )
        applied.append(ev)

    rewritten = tree.sql(dialect=dialect)

    if has_complex:
        if llm_fallback is not None and schema_text is not None:
            return {
                "sql": llm_fallback(rewritten, diff_events, schema_text),
                "method": "llm",
                "applied": applied,
            }

    return {"sql": rewritten, "method": "ast", "applied": applied}
