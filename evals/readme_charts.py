#!/usr/bin/env python3
"""Render the two plain-language README charts from the frozen benchmark ledger.

Run from any directory after installing requirements-benchmarks.txt. Only the
README PNGs are replaced; the historical report and its figures stay unchanged.
"""
import json
from math import floor
from pathlib import Path
from statistics import median

from benchmark_charts import NAMES, setup_plotting
from benchmark_data import ARMS, SAMPLES, require, summarize

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "evals/results/2026-09-17-benchmarks/benchmark.json"
OUTPUT = ROOT / "docs/images"
LABELS = {
    "raw": "Without Minimap",
    "new_graph": "Record a route",
    "reused_graph": "Reuse a route",
}
COLORS = {"raw": "#536A88", "new_graph": "#AD7419", "reused_graph": "#087F72"}
INK = "#182A3A"
MUTED = "#52616E"


def load_medians():
    data = json.loads(LEDGER.read_text())
    require(data["schema_version"] == 1, "Unsupported benchmark schema")
    require(data["groups"] == summarize(data["trials"], data["skill"]["tokens"]),
            "Ledger summaries do not match its trials")
    require(len(data["trials"]) == 90 and len(data["groups"]) == 6
            and {g["api"] for g in data["groups"]} == {"36", "37"}
            and all(g["repeats"] == [1, 2, 3, 4, 5] for g in data["groups"]),
            "README charts describe the original 90-trial, two-emulator suite")
    return {
        sample: {
            arm: {
                metric: median(t[metric] for t in data["trials"]
                               if t["sample"] == sample and t["arm"] == arm)
                for metric in ("seconds", "o200k_base")
            }
            for arm in ARMS
        }
        for sample in SAMPLES
    }


def draw_chart(values, metric, plt):
    from matplotlib.patches import Patch
    from matplotlib.ticker import StrMethodFormatter

    tokens = metric == "o200k_base"
    arms = ("raw", "reused_graph") if tokens else ARMS
    title = ("Saved routes return much less text" if tokens
             else "Recording a route takes extra time")
    subtitle = ("Shorter bars mean less text for the AI to read." if tokens
                else "Reusing is quicker than recording, but a little slower than using no Minimap.")
    footnote = ("Counts tool replies only. Total AI usage and money saved have not been measured."
                if tokens else "Navigation time only. App startup and separate test checks are not included.")

    fig, ax = plt.subplots(figsize=(10.5, 7.2))
    fig.subplots_adjust(left=.18, right=.91, top=.73, bottom=.28)
    fig.text(.045, .95, title, fontsize=24, fontweight="bold", va="top")
    fig.text(.045, .88, subtitle, fontsize=12.5, color=MUTED, va="top")
    fig.legend(handles=[Patch(color=COLORS[a], label=LABELS[a]) for a in arms],
               loc="upper left", bbox_to_anchor=(.035, .827), ncol=len(arms),
               frameon=False, fontsize=13, handlelength=1.3, columnspacing=1.8)

    limit = 2300 if tokens else 34
    spacing = .27 if tokens else .23
    for i, sample in enumerate(SAMPLES):
        for j, arm in enumerate(arms):
            value = values[sample][arm][metric]
            y = i + (j - (len(arms) - 1) / 2) * spacing
            ax.barh(y, value, height=spacing * .78, color=COLORS[arm], zorder=3)
            label = f"{floor(value + .5):,}" if tokens else f"{value:.1f} s"
            if tokens and arm == "reused_graph":
                reduction = 100 * (1 - value / values[sample]["raw"][metric])
                label += f"  ({reduction:.0f}% less)"
            ax.text(value + limit * .015, y, label, va="center", fontsize=13,
                    color=INK, fontweight="bold" if arm == "reused_graph" else "normal")

    ax.set_yticks(range(len(SAMPLES)), [NAMES[s] for s in SAMPLES],
                  fontsize=14, fontweight="bold", color=INK)
    ax.tick_params(axis="y", length=0, pad=12)
    ax.tick_params(axis="x", labelsize=12, length=0, pad=8)
    ax.set_ylim(len(SAMPLES) - .5, -.5)
    ax.set_xlim(0, limit)
    ax.set_xticks([0, 500, 1000, 1500, 2000] if tokens else [0, 10, 20, 30])
    ax.xaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    ax.set_xlabel("Text chunks (tokens)" if tokens else "Seconds", fontsize=13, labelpad=12)
    ax.grid(axis="x", color="#E6EBEE", zorder=0)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    fig.text(.045, .085,
             "Typical result: the middle of 10 runs per app and method, combining both Android versions.\n"
             + footnote, fontsize=11.5, color=MUTED, linespacing=1.65, va="bottom")

    name = "benchmark-text.png" if tokens else "benchmark-time.png"
    fig.savefig(OUTPUT / name, dpi=170, metadata={"Software": "Minimap README benchmark"})
    plt.close(fig)


def main():
    values = load_medians()
    plt = setup_plotting()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for metric in ("o200k_base", "seconds"):
        draw_chart(values, metric, plt)
    print(json.dumps({"source": str(LEDGER.relative_to(ROOT)), "medians": values}, indent=2))


if __name__ == "__main__":
    main()
