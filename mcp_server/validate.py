"""query/validate Tool — three-state verdict (valid / execution_error / silent_failure_suspected).

Two back-translation similarity backends are available:

  v1 (token): H3 compares a deterministic sqlglot SQL2Text paraphrase to the
      original question through token-set Jaccard similarity (plus synonym
      expansion). Feasibility stub — zero precision/recall on the pilot
      TABLE_RENAME subset (see README §9).
  v2 (embedding): the same paraphrase is compared to the question through
      sentence-embedding cosine similarity (sentence-transformers MiniLM,
      local, zero API cost). Selected via ``validate_v2`` or by passing
      ``similarity_fn=`` to ``validate``.

Heuristics H1 (arity) and H2 (empty-result-on-affirmative-question) are common
to both backends.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

import sqlglot
from sqlglot import exp


VALID = "valid"
EXEC_ERROR = "execution_error"
SILENT = "silent_failure_suspected"


@dataclass
class ValidationResult:
    verdict: str
    rows: list[tuple] | None
    row_count: int | None
    error: str | None
    paraphrase: str | None
    similarity: float | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "row_count": self.row_count,
            "error": self.error,
            "paraphrase": self.paraphrase,
            "similarity": self.similarity,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

# Lightweight question/SQL synonym map. The thesis pipeline replaces this
# with sentence-embedding cosine similarity (sentence-transformers).
_SYNONYM_MAP = {
    "many":     {"count", "number"},
    "number":   {"count"},
    "how":      {"count"},
    "name":     {"named"},
    "names":    {"name"},
    "song":     {"songs", "track", "title"},
    "songs":    {"song"},
    "year":     {"years"},
    "list":     {"show", "display"},
    "show":     {"list", "display"},
    "find":     {"show", "list"},
    "average":  {"avg", "mean"},
    "smallest": {"min", "minimum"},
    "largest":  {"max", "maximum"},
    "oldest":   {"max"},
    "youngest": {"min"},
    "people":   {"singer", "singers", "artist", "artists"},
    "singer":   {"singers", "artist", "artists"},
    "singers":  {"singer", "artist", "artists"},
    "artist":   {"singer", "singers", "artists"},
    "artists":  {"singer", "singers", "artist"},
    "release":  {"released"},
}

_STOP = {
    "the", "a", "an", "of", "in", "on", "and", "or", "is", "are",
    "was", "were", "for", "to", "from", "with", "by", "all", "any",
    "their", "every", "each", "do", "does", "we", "have",
}


def _tokens(text: str) -> set[str]:
    raw = set(re.findall(r"[a-z]+", text.lower())) - _STOP
    expanded = set(raw)
    for t in raw:
        expanded |= _SYNONYM_MAP.get(t, set())
    return expanded


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


def sql_to_text(sql: str, dialect: str = "sqlite") -> str:
    """Deterministic sqlglot-based AST-to-NL paraphrase.

    Produces a coarse but keyword-aligned paraphrase of the SQL useful for
    token-overlap checks. The thesis pipeline replaces this with a small
    instruction-tuned T5 / Llama-3.2-3B back-translator.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return ""
    select = tree.find(exp.Select)
    if select is None:
        return ""
    parts: list[str] = ["show"]
    # COUNT / AVG / MIN / MAX projection cues — these are the main NL anchors.
    aggs = list(tree.find_all(exp.Count)) + list(tree.find_all(exp.Avg)) \
         + list(tree.find_all(exp.Min)) + list(tree.find_all(exp.Max))
    if any(isinstance(p, exp.Count) for p in aggs):
        parts.append("count")
    if any(isinstance(p, exp.Avg) for p in aggs):
        parts.append("average")
    if any(isinstance(p, exp.Min) for p in aggs):
        parts.append("min")
    if any(isinstance(p, exp.Max) for p in aggs):
        parts.append("max")
    # Projected column names (lowercase, underscores split).
    for p in select.selects:
        nm = p.alias_or_name or ""
        for piece in re.split(r"[_\W]+", nm):
            if piece:
                parts.append(piece.lower())
    # WHERE / ORDER / GROUP cues.
    if select.args.get("where"):
        parts.append("where")
        for col in select.args["where"].find_all(exp.Column):
            for piece in re.split(r"[_\W]+", col.name or ""):
                if piece:
                    parts.append(piece.lower())
    if select.args.get("order"):
        parts.append("ordered")
    if select.args.get("group"):
        parts.append("grouped")
    # Table names — useful only if NL question mentions them (otherwise stop-listed).
    for t in tree.find_all(exp.Table):
        if t.name:
            parts.append(t.name.lower())
    return " ".join(parts)


_AFFIRMATIVE_CUES = re.compile(
    r"\b(?:how\s+many|count|list|what|which|find|show|name|who|where|"
    r"number\s+of|total)\b",
    re.IGNORECASE,
)


def _affirmative(question: str) -> bool:
    """Return True if the question contains cues implying a non-zero answer."""
    return bool(_AFFIRMATIVE_CUES.search(question))


def _is_zero_count(rows: list[tuple], sql: str) -> bool:
    """Return True if *rows* is a single-row aggregate whose value is zero.

    This catches the COUNT(*) = 0 case that ``len(rows) == 0`` misses
    because aggregate queries always return exactly one row.
    """
    if len(rows) != 1 or len(rows[0]) != 1:
        return False
    val = rows[0][0]
    if val == 0 or val == "0":
        # Confirm that the SQL actually contains an aggregate function
        # so we don't mis-fire on a non-aggregate single-column result.
        try:
            tree = sqlglot.parse_one(sql, read="sqlite")
            aggs = (
                list(tree.find_all(exp.Count))
                + list(tree.find_all(exp.Sum))
                + list(tree.find_all(exp.Avg))
            )
            return len(aggs) > 0
        except Exception:
            return False
    return False


# --------------------------------------------------------------------------- #
# v2: sentence-embedding similarity backend                                   #
# --------------------------------------------------------------------------- #

# Default cosine-similarity threshold for the embedding backend. Token-Jaccard
# and embedding-cosine live on different scales, hence the separate default.
# Locked to 0.45 on 2026-07-10 after the G1 threshold sweep on the 60-case
# labelled SFD set: 0.35 → P 1.00 / R 0.25, 0.40 → P 1.00 / R 0.325,
# 0.45 → P 1.00 / R 0.375 (best recall at zero false positives; beats the
# v1 token baseline P 0.92 / R 0.30 on both axes).
EMBEDDING_THRESHOLD = 0.45

# Lazy singleton — the model (~80 MB, all-MiniLM-L6-v2) is only loaded when the
# embedding backend is actually requested, so the token path and the offline
# test suite never import sentence-transformers.
_EMBEDDER = None


def _load_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        from sentence_transformers import SentenceTransformer  # noqa: import-outside-toplevel
        _EMBEDDER = SentenceTransformer("all-MiniLM-L6-v2")
    return _EMBEDDER


def embedding_similarity(a: str, b: str) -> float:
    """Cosine similarity of the two texts under MiniLM sentence embeddings.

    Requires ``pip install sentence-transformers`` (local inference, no API
    calls). Raises ImportError if the package is missing — callers decide
    whether to fall back to the token backend.
    """
    model = _load_embedder()
    va, vb = model.encode([a, b], normalize_embeddings=True)
    return float((va * vb).sum())


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #

def validate(
    db_path: str,
    sql: str,
    original_question: str,
    similarity_threshold: float = 0.10,
    similarity_fn: Callable[[str, str], float] | None = None,
) -> ValidationResult:
    # 1. Execution.
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        rows = cur.execute(sql).fetchall()
        con.close()
    except Exception as exc:
        return ValidationResult(
            verdict=EXEC_ERROR,
            rows=None,
            row_count=None,
            error=str(exc),
            paraphrase=None,
            similarity=None,
        )

    # 2. SQL2Text back-translation similarity (H3).
    #    v1: token-set Jaccard (default). v2: caller-supplied similarity_fn,
    #    e.g. ``embedding_similarity`` (sentence-embedding cosine).
    para = sql_to_text(sql)
    if similarity_fn is not None:
        sim = similarity_fn(para, original_question)
        low_sim_reason = "low_semantic_similarity"
    else:
        sim = _jaccard(_tokens(para), _tokens(original_question))
        low_sim_reason = "low_token_overlap"

    suspect_sim = sim < similarity_threshold
    if suspect_sim:
        return ValidationResult(
            verdict=SILENT,
            rows=rows,
            row_count=len(rows),
            error=None,
            paraphrase=para,
            similarity=sim,
            reason=low_sim_reason,
        )

    # 3. Empty-result on affirmative question (H2).
    #    Covers both truly empty result-sets AND aggregate queries
    #    (e.g. COUNT(*)) that return a single zero-valued row.
    suspect_empty = (
        _affirmative(original_question)
        and (len(rows) == 0 or _is_zero_count(rows, sql))
    )
    if suspect_empty:
        return ValidationResult(
            verdict=SILENT,
            rows=rows,
            row_count=len(rows),
            error=None,
            paraphrase=para,
            similarity=sim,
            reason="empty_result_on_affirmative_question",
        )

    return ValidationResult(
        verdict=VALID,
        rows=rows,
        row_count=len(rows),
        error=None,
        paraphrase=para,
        similarity=sim,
    )


def validate_v2(
    db_path: str,
    sql: str,
    original_question: str,
    similarity_threshold: float = EMBEDDING_THRESHOLD,
    similarity_fn: Callable[[str, str], float] | None = None,
) -> ValidationResult:
    """validate with the sentence-embedding back-translation backend (v2).

    ``similarity_fn`` may be injected for testing; by default the MiniLM
    embedding backend is used (local, no API cost). Falls back to the v1
    token backend with a warning if sentence-transformers is unavailable,
    so a missing optional dependency can never crash a paid API run.
    """
    fn = similarity_fn
    if fn is None:
        try:
            _load_embedder()
            fn = embedding_similarity
        except ImportError:
            import warnings
            warnings.warn(
                "sentence-transformers not installed — validate_v2 falling "
                "back to the v1 token backend. `pip install "
                "sentence-transformers` to enable the embedding backend.",
                RuntimeWarning,
                stacklevel=2,
            )
            return validate(db_path, sql, original_question)
    return validate(
        db_path,
        sql,
        original_question,
        similarity_threshold=similarity_threshold,
        similarity_fn=fn,
    )
