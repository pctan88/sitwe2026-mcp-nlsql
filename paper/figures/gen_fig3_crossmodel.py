"""Generate Figure 3 — Cross-model grouped bar chart for journal version.

4 model groups × 3 bars (Stale / MCP / Refreshed-ceiling)
concert_singer, all five operators pooled, Wilson 95% CI on MCP bars.
RR annotations above each MCP bar.
"""
from __future__ import annotations
import math
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

OUTDIR = Path(__file__).resolve().parent

# Verified counts from per-operator JSONs (concert_singer, all 5 ops, n=76)
models_data = [
    ("Haiku 4.5",          76, 8,  54, 45, 0.80),
    ("GPT-4o",             76, 12, 62, 60, 0.96),
    ("Gemini 2.5\nFlash",  76, 12, 67, 63, 0.93),
    ("Llama 3.3\n70B",     76, 12, 65, 61, 0.93),
]

def wilson(k, n, z=1.96):
    if n == 0: return 0.0, 1.0
    p = k/n; z2 = z*z; den = 1 + z2/n
    mid = (p + z2/(2*n)) / den
    half = z * math.sqrt(p*(1-p)/n + z2/(4*n*n)) / den
    return max(0.0, mid-half), min(1.0, mid+half)

plt.rcParams.update({
    "font.family": "serif", "font.serif": ["Times New Roman","Times","DejaVu Serif"],
    "font.size": 8, "axes.labelsize": 8, "xtick.labelsize": 8,
    "ytick.labelsize": 7.5, "figure.dpi": 300, "axes.linewidth": 0.6,
})

fig, ax = plt.subplots(figsize=(3.45, 2.9))

n_models = len(models_data)
x = np.arange(n_models)
w = 0.22

col_stale = "#CCCCCC"
col_mcp   = "#222222"
col_refr  = "#777777"

for i, (name, n, n_stale, n_refr, n_mcp, rr) in enumerate(models_data):
    ex_stale = n_stale / n
    ex_refr  = n_refr  / n
    ex_mcp   = n_mcp   / n
    mcp_lo, mcp_hi = wilson(n_mcp, n)

    # Stale bar
    ax.bar(x[i] - w, ex_stale, width=w, color=col_stale, edgecolor="#000",
           linewidth=0.5, zorder=3)
    # MCP bar with CI
    ax.bar(x[i], ex_mcp, width=w, color=col_mcp, edgecolor="#000",
           linewidth=0.5, zorder=3,
           yerr=[[ex_mcp - mcp_lo], [mcp_hi - ex_mcp]],
           capsize=2.5, error_kw={"elinewidth":0.7,"ecolor":"#555","capthick":0.7})
    # Refreshed ceiling bar
    ax.bar(x[i] + w, ex_refr, width=w, color=col_refr, edgecolor="#000",
           linewidth=0.5, zorder=3)

    # EX value labels (small, above each bar)
    for xpos, val in [(x[i]-w, ex_stale), (x[i]+w, ex_refr)]:
        ax.text(xpos, val + 0.022, f"{val:.2f}", ha="center", va="bottom",
                fontsize=5, fontfamily="serif", color="#444")

    # RR annotation above MCP bar — bold, white-boxed
    rr_y = mcp_hi + 0.055
    ax.text(x[i], rr_y, f"RR={rr:.2f}", ha="center", va="bottom",
            fontsize=5.5, fontfamily="serif", fontstyle="italic",
            color="#111",
            bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="#AAA",
                      linewidth=0.4, alpha=0.9))
    # MCP EX value inside/below annotation
    ax.text(x[i], ex_mcp + 0.018, f"{ex_mcp:.2f}", ha="center", va="bottom",
            fontsize=5, fontfamily="serif", color="white")

ax.set_xticks(x)
ax.set_xticklabels([m[0] for m in models_data], linespacing=1.2)
ax.set_ylabel("Execution Accuracy (EX)", labelpad=4)
ax.set_ylim(0, 1.13)
ax.yaxis.set_major_locator(plt.MultipleLocator(0.2))
ax.grid(axis="y", which="major", linewidth=0.4, linestyle="--",
        color="#CCCCCC", zorder=0)
ax.set_axisbelow(True)
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)

# Legend
from matplotlib.patches import Patch
legend_elements = [
    Patch(facecolor=col_stale, edgecolor="#000", linewidth=0.5, label="Stale (baseline)"),
    Patch(facecolor=col_mcp,   edgecolor="#000", linewidth=0.5, label="MCP-mediated"),
    Patch(facecolor=col_refr,  edgecolor="#000", linewidth=0.5, label="Refreshed (ceiling)"),
]
ax.legend(handles=legend_elements, fontsize=5.5, loc="upper left",
          framealpha=0.85, edgecolor="#CCC", handlelength=1.0,
          handletextpad=0.4, borderpad=0.4)

fig.text(
    0.5, -0.01,
    "concert_singer, all 5 operators, N=76. "
    "Error bars: Wilson 95% CI on MCP bars. "
    "Haiku 4.5 lower absolute EX reflects smaller model capacity;\n"
    "Recovery Rate 0.80 is consistent with other clients. "
    "hr₁ results in Table V.",
    ha="center", va="top", fontsize=5.2, fontstyle="italic",
    fontfamily="serif", color="#555", wrap=True
)

fig.tight_layout(pad=0.5)

for ext, kw in [("png", {"dpi":300,"bbox_inches":"tight"}),
                ("pdf", {"bbox_inches":"tight"})]:
    out = OUTDIR / f"fig_crossmodel_bars.{ext}"
    fig.savefig(out, **kw)
    print(f"[ok] {out}")
plt.close(fig)
