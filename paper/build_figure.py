"""Render Figure 1 (runtime architecture + call flow) as a PNG for the .docx.

v2 of the figure — addresses the second reviewer's "very low information
density" critique on the original five-box pipeline. Adds:

  - the BLAKE3 hash compute step inside ``schema/fingerprint``;
  - a numbered call sequence (1)..(9) on the happy-path arrows;
  - the dashed LLM-assisted fallback arrow from ``query/relink`` back to
    the LLM Client (fires for TABLE_SPLIT / COLUMN_MERGE-with-expression
    operators the AST path cannot rewrite);
  - the dashed re-prompt arrow from ``query/validate`` back to the LLM
    Client (fires when verdict = silent_failure_suspected);
  - separate database arrows for *introspect* (PRAGMA, by
    schema/fingerprint) and *execute* (by query/validate).

Run::

    python paper/build_figure.py        # writes paper/fig_arch.png
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches

HERE = Path(__file__).resolve().parent
OUT  = HERE / "fig_arch.png"


# --------------------------------------------------------------------------- #
# Layout constants                                                            #
# Y goes 0 (bottom) -> 1 (top). Each layer's (y_bottom, height) is fixed       #
# here so all coordinates downstream stay consistent.                          #
# --------------------------------------------------------------------------- #

L1_Y, L1_H = 0.93, 0.06   # User
L2_Y, L2_H = 0.78, 0.12   # LLM Client            (gap of 0.03 above)
L3_Y, L3_H = 0.27, 0.40   # MCP Server  (gap of 0.11 above for labels)
L4_Y, L4_H = 0.09, 0.13   # Database              (gap of 0.05 above)

# Primitive box geometry inside Layer 3.
PRIM_Y, PRIM_H = 0.30, 0.32   # 0.30 .. 0.62

# Y of horizontal arrows between adjacent layers (inter-layer gap).
GAP_12_TOP = L1_Y                  # bottom of Layer 1   = 0.91 (top edge of gap)
GAP_12_BOT = L2_Y + L2_H           # top of Layer 2      = 0.86

GAP_23_TOP = L2_Y                  # bottom of Layer 2   = 0.73
GAP_23_BOT = L3_Y + L3_H           # top of Layer 3      = 0.68
# Note: GAP_23_TOP > GAP_23_BOT so arrows go downward (top→bot).

GAP_34_TOP = L3_Y                  # bottom of Layer 3   = 0.30
GAP_34_BOT = L4_Y + L4_H           # top of Layer 4      = 0.23


# --------------------------------------------------------------------------- #
# Drawing primitives                                                          #
# --------------------------------------------------------------------------- #

def _layer(ax, x, y, w, h, title, body, *, fill, edge="#222"):
    """Draw a layer box with a title at top-left and optional multi-line body.

    Text offsets are computed from the TOP edge of the box (y + h - off) so
    they remain visually consistent across layers of different heights.
    """
    box = patches.FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.005,rounding_size=0.02",
        linewidth=1.0, edgecolor=edge, facecolor=fill,
    )
    ax.add_patch(box)
    ax.text(x + 0.015, y + h - 0.022, title,
            fontsize=9, weight="bold", ha="left", va="top")
    if body:
        ax.text(x + 0.015, y + h - 0.045, body,
                fontsize=7.5, ha="left", va="top", wrap=True)


def _primitive(ax, x, y, w, h, name, kind, body):
    box = patches.FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.005,rounding_size=0.015",
        linewidth=0.8, edgecolor="#444", facecolor="#fff",
    )
    ax.add_patch(box)
    ax.text(x + w / 2, y + h - 0.020, name,
            fontsize=8, weight="bold", ha="center", va="top",
            family="monospace")
    ax.text(x + w / 2, y + h - 0.040, kind,
            fontsize=6.5, ha="center", va="top", color="#666")
    ax.text(x + w / 2, y + h - 0.060, body,
            fontsize=6.5, ha="center", va="top")


def _flow(ax, x1, y1, x2, y2, *,
          label: str = "", num: str | None = None,
          dashed: bool = False, color: str = "#333",
          label_xoff: float = 0.012, label_yoff: float = 0.0,
          label_ha: str = "left",
          curved: bool = False, rad: float = 0.0):
    """Draw a labelled flow arrow with an optional circled step number.

    label_xoff / label_yoff position the label relative to the midpoint;
    label_ha lets us flip alignment so the label sits cleanly to one side
    of the arrow.
    """
    style = "->"
    linestyle = (0, (4, 2)) if dashed else "-"
    connectionstyle = f"arc3,rad={rad}" if curved else "arc3,rad=0"
    ax.annotate(
        "",
        xy=(x2, y2), xytext=(x1, y1),
        arrowprops=dict(arrowstyle=style, lw=1.2, color=color,
                        linestyle=linestyle,
                        connectionstyle=connectionstyle),
    )
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    if num is not None:
        circ = patches.Circle((mx, my), 0.0115,
                              edgecolor=color, facecolor="white", linewidth=1.0,
                              zorder=5)
        ax.add_patch(circ)
        ax.text(mx, my, num, fontsize=6, ha="center", va="center",
                color=color, zorder=6, weight="bold")
    if label:
        ax.text(mx + label_xoff, my + label_yoff, label,
                fontsize=6.5, ha=label_ha, va="center",
                color=("#b45309" if dashed else "#555"),
                style="italic")


# --------------------------------------------------------------------------- #
# Figure                                                                      #
# --------------------------------------------------------------------------- #

def build() -> None:
    fig, ax = plt.subplots(figsize=(7.6, 6.0), dpi=300)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # ---- Layer 1 — User ------------------------------------------------ #
    _layer(ax, 0.05, L1_Y, 0.90, L1_H,
           "Layer 1 — User",
           "Natural-language question (NL).",
           fill="#eef2ff")

    # ---- Layer 2 — LLM Client ----------------------------------------- #
    _layer(ax, 0.05, L2_Y, 0.90, L2_H,
           "Layer 2 — LLM Client (off-the-shelf, MCP-compatible)",
           "Claude Haiku 4.5  ·  claude-haiku-4-5-20251001  ·  used as "
           "released, no fine-tuning.\n"
           "Holds the MCP session and orchestrates the three primitive "
           "calls below.",
           fill="#dbeafe")

    # ---- Layer 3 — MCP Server (container) ----------------------------- #
    _layer(ax, 0.05, L3_Y, 0.90, L3_H,
           "Layer 3 — MCP Server  (proposed contribution, RC1)",
           "",
           fill="#fce7f3")

    _primitive(ax, 0.075, PRIM_Y, 0.265, PRIM_H,
               "schema/fingerprint", "Resource",
               "BLAKE3 256-bit\n"
               "canonical hash\n"
               "F(S) = H(⊔ name(T) ‖\n"
               "      ⊔ (name(C) ‖ type(C)))\n"
               "+ diff classification over\n"
               "the five EvoSchema\n"
               "operator types")

    _primitive(ax, 0.365, PRIM_Y, 0.265, PRIM_H,
               "query/relink", "Tool",
               "sqlglot AST rewrite for\n"
               "table rename, column\n"
               "rename, table merge,\n"
               "column merge\n"
               "→ LLM-assisted fallback\n"
               "for TABLE_SPLIT and\n"
               "COLUMN_MERGE-with-\n"
               "expression")

    _primitive(ax, 0.66, PRIM_Y, 0.265, PRIM_H,
               "query/validate", "Tool",
               "exec + result-set arity\n"
               "check + SQL-to-text\n"
               "back-translation +\n"
               "empty-on-affirmative\n"
               "cue → verdict in\n"
               "{valid, exec_error,\n"
               "silent_suspected}")

    # ---- Layer 4 — Database ------------------------------------------- #
    _layer(ax, 0.05, L4_Y, 0.90, L4_H,
           "Layer 4 — Database  (read-only SQLite)",
           "Spider 1.0 / BIRD / EvoSchema corpora.\n"
           "concert_singer fixture bundled for the pilot.",
           fill="#dcfce7")

    # ------------------------------------------------------------------ #
    # Numbered happy-path flow                                            #
    # ------------------------------------------------------------------ #

    # 1: User -> LLM Client  (NL query enters the session)
    _flow(ax, 0.50, GAP_12_TOP, 0.50, GAP_12_BOT,
          label="NL query", num="1")

    # 2 + 4: bidirectional schema-fingerprint round-trip.
    # Drawn as two side-by-side vertical arrows; labels sit outside the
    # arrow pair so they don't crowd the Layer 3 title.
    _flow(ax, 0.175, GAP_23_TOP, 0.175, PRIM_Y + PRIM_H,
          label="read", num="2",
          label_xoff=-0.012, label_ha="right", label_yoff=0.0)
    _flow(ax, 0.215, PRIM_Y + PRIM_H, 0.215, GAP_23_TOP,
          label="schema\n+ diff", num="4",
          label_xoff=0.012, label_ha="left", label_yoff=0.0)

    # 3: schema/fingerprint -> DB  (PRAGMA introspect)
    _flow(ax, 0.175, PRIM_Y, 0.175, GAP_34_BOT,
          label="introspect", num="3",
          label_xoff=-0.012, label_ha="right")

    # 3-return: DB -> schema/fingerprint
    _flow(ax, 0.215, GAP_34_BOT, 0.215, PRIM_Y,
          label="meta rows", color="#888",
          label_xoff=0.012, label_yoff=0.0)

    # 5: LLM Client -> query/relink  (stale SQL + diff)
    _flow(ax, 0.485, GAP_23_TOP, 0.485, PRIM_Y + PRIM_H,
          label="stale SQL", num="5",
          label_xoff=-0.012, label_ha="right")

    # 6: query/relink -> query/validate (rewritten SQL)
    _flow(ax, 0.630, PRIM_Y + PRIM_H * 0.5,
          0.660, PRIM_Y + PRIM_H * 0.5,
          label="rewritten SQL", num="6",
          label_xoff=0.0, label_yoff=0.022, label_ha="center")

    # 7: query/validate -> DB
    _flow(ax, 0.785, PRIM_Y, 0.785, GAP_34_BOT,
          label="execute", num="7",
          label_xoff=-0.012, label_ha="right")

    # 7-return: DB -> query/validate
    _flow(ax, 0.825, GAP_34_BOT, 0.825, PRIM_Y,
          label="rows", color="#888",
          label_xoff=0.012, label_yoff=0.0)

    # 8: query/validate -> LLM Client  (verdict + rows)
    _flow(ax, 0.745, PRIM_Y + PRIM_H, 0.745, GAP_23_TOP,
          label="verdict\n+ rows", num="8",
          label_xoff=0.012, label_ha="left")

    # 1: User -> LLM Client (already drawn above)
    # 9: LLM Client -> User (already drawn above)

    # ------------------------------------------------------------------ #
    # Conditional dashed arrows                                           #
    # ------------------------------------------------------------------ #

    # query/relink → LLM Client  (LLM-assisted fallback for complex ops)
    _flow(ax, 0.435, PRIM_Y + PRIM_H, 0.435, GAP_23_TOP,
          label="LLM\nfallback",
          dashed=True, color="#b45309",
          label_xoff=-0.012, label_ha="right", label_yoff=0.0)

    # query/validate → LLM Client  (re-prompt on silent failure)
    _flow(ax, 0.880, PRIM_Y + PRIM_H, 0.880, GAP_23_TOP,
          label="re-prompt\non suspect",
          dashed=True, color="#b45309",
          label_xoff=0.012, label_ha="left", label_yoff=0.0)

    # ------------------------------------------------------------------ #
    # Legend                                                              #
    # ------------------------------------------------------------------ #
    ax.text(0.05, 0.055,
            "—  happy-path call  (steps 1–9, MCP / JSON-RPC 2.0)",
            fontsize=6.5, ha="left", va="center", color="#333")
    ax.text(0.05, 0.035,
            "·· dashed  conditional fallback / re-prompt path",
            fontsize=6.5, ha="left", va="center", color="#b45309")
    ax.text(0.05, 0.015,
            "grey return arrows  =  data flowing back from the database",
            fontsize=6.5, ha="left", va="center", color="#888")

    # ------------------------------------------------------------------ #
    # Caption                                                             #
    # ------------------------------------------------------------------ #
    fig.text(0.5, -0.01,
             "Fig. 1.  Runtime architecture and call flow. The LLM client "
             "is used as released; all schema-evolution awareness is added "
             "at the MCP-server layer. Happy path is the numbered "
             "sequence (1)→(9); the two dashed orange arrows mark "
             "conditional paths — the LLM-assisted relink fallback for "
             "operators outside the AST rewriter's coverage, and the "
             "re-prompt loop fired when query/validate flags a suspected "
             "silent failure.",
             ha="center", fontsize=7.5, style="italic", wrap=True)

    fig.savefig(OUT, dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[ok] wrote {OUT}  ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
