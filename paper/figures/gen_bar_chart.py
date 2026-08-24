"""Generate Figure 2 — Execution Accuracy bar chart for SITWE 2026 paper.

Hardcoded pooled values: 76 queries, all 5 EvoSchema operators, concert_singer, Haiku 4.5.
  stale=8/76=0.105, refreshed=54/76=0.711, error-feedback=8/76=0.105, mcp=45/76=0.592

Re-run any time the pilot data changes:
    cd Pilot_Study_SITWE2026
    python paper/figures/gen_bar_chart.py
"""
from __future__ import annotations
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE   = Path(__file__).resolve().parent
OUTDIR = HERE

# Pooled 76-query values (all 5 operators, concert_singer, Haiku 4.5)
POOL_N = 76
configs = [
    ("Baseline\n(stale schema)",      8,  "#AAAAAA"),
    ("Refreshed-schema\n(ceiling)",   54, "#666666"),
    ("Error-feedback\n(1 retry)",      8,  "#BBBBBB"),
    ("MCP-mediated\n(proposed)",      45, "#222222"),
]
labels  = [c[0] for c in configs]
counts  = [c[1] for c in configs]
colours = [c[2] for c in configs]
values  = [k / POOL_N for k in counts]

def wilson(k: int, n: int, z: float = 1.96):
    if n == 0: return 0.0, 1.0
    p   = k / n; z2 = z * z; den = 1 + z2 / n
    mid = (p + z2 / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)

cis = [wilson(k, POOL_N) for k in counts]
lo_errs = [v - lo for v, (lo, hi) in zip(values, cis)]
hi_errs = [hi - v  for v, (lo, hi) in zip(values, cis)]

plt.rcParams.update({
    "font.family": "serif",
    "font.serif":  ["Times New Roman", "Times", "DejaVu Serif"],
    "font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 8,
    "xtick.labelsize": 7, "ytick.labelsize": 7.5,
    "figure.dpi": 300, "axes.linewidth": 0.6,
})

fig, ax = plt.subplots(figsize=(3.45, 2.6))
x = np.arange(len(labels)); w = 0.55

bars = ax.bar(x, values, width=w, color=colours, edgecolor="#000000",
    linewidth=0.6, yerr=[lo_errs, hi_errs], capsize=3,
    error_kw={"elinewidth": 0.8, "ecolor": "#333333", "capthick": 0.8}, zorder=3)

for bar, val, hi in zip(bars, values, hi_errs):
    ax.text(bar.get_x() + bar.get_width() / 2, val + hi + 0.03,
            f"{val:.2f}", ha="center", va="bottom", fontsize=6.5, fontfamily="serif")

ax.set_xticks(x); ax.set_xticklabels(labels, linespacing=1.2)
ax.set_ylabel("Execution Accuracy (EX)", labelpad=4)
ax.set_ylim(0, 1.08)
ax.yaxis.set_major_locator(plt.MultipleLocator(0.2))
ax.yaxis.set_minor_locator(plt.MultipleLocator(0.1))
ax.grid(axis="y", which="major", linewidth=0.4, linestyle="--", color="#CCCCCC", zorder=0)
ax.set_axisbelow(True)
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)

# RR arrow: from top of MCP bar toward baseline, annotated RR=0.80
rr = (values[3] - values[0]) / (values[1] - values[0])
ax.annotate("", xy=(x[3] - w/2, values[3]), xycoords="data",
    xytext=(x[0] + w/2, values[0] + 0.02), textcoords="data",
    arrowprops=dict(arrowstyle="-|>", color="#333333", lw=0.8, mutation_scale=7))
ax.text((x[0]+x[3])/2, (values[0]+values[3])/2 + 0.12, f"RR = {rr:.2f}",
    ha="center", va="bottom", fontsize=6.5, fontstyle="italic",
    fontfamily="serif", color="#333333",
    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.8))

fig.text(0.5, -0.01, f"Error bars: Wilson 95% CI  (N = {POOL_N} queries, all 5 operators)",
    ha="center", va="top", fontsize=6, fontstyle="italic",
    fontfamily="serif", color="#444444")

fig.tight_layout(pad=0.4)
for ext in ("png", "pdf"):
    out = OUTDIR / f"fig_ex_bar.{ext}"
    kw = {"dpi": 300, "bbox_inches": "tight"} if ext == "png" else {"bbox_inches": "tight"}
    fig.savefig(out, **kw)
    print(f"[ok] {out}")
plt.close(fig)
