#!/usr/bin/env python3
"""Render every replay time from an analyzed cohort; no device or agent work."""
import argparse
import json
from pathlib import Path

from analyze_replay import ARMS, successful
from benchmark_charts import NAMES, setup_plotting

COLORS = {"raw": "#37659B", "baseline": "#B16B31", "candidate": "#087F72"}
LABELS = ("Without\nMinimap", "Released\nMinimap", "Candidate")


def render(data, output):
    plt = setup_plotting()
    apis = sorted({group["api"] for group in data["groups"]})
    samples = ["jetsnack", "jetnews", "jetchat"]
    fig, axes = plt.subplots(len(apis), 3, figsize=(12, 3.7 * len(apis) + 2.4),
                             squeeze=False, sharey=True)
    fig.subplots_adjust(top=1 - 1.2 / fig.get_figheight(), bottom=1.35 / fig.get_figheight(),
                        left=.07, right=.98, hspace=.55, wspace=.22)
    rows = [row for row in data["trials"] if successful(row)]
    highest = max((row["seconds"] for row in rows), default=1) * 1.2
    for y, api in enumerate(apis):
        for x, sample in enumerate(samples):
            ax = axes[y, x]
            group = next((g for g in data["groups"] if g["api"] == api and g["sample"] == sample), None)
            ax.set_title(f"{NAMES[sample]} · API {api}", loc="left", fontweight="bold")
            ax.set_xticks(range(3), LABELS)
            ax.set_ylim(0, highest)
            ax.grid(axis="y", color="#E6EBEE")
            ax.set_axisbelow(True)
            if x == 0:
                ax.set_ylabel("Seconds · lower is better")
            if group is None:
                ax.text(.5, .5, "Not measured", transform=ax.transAxes, ha="center")
                continue
            for index, arm in enumerate(ARMS):
                stats = group["arms"][arm]
                m = stats["median_successful_seconds"]
                values = [r["seconds"] for r in rows if r["api"] == api and r["sample"] == sample and r["arm"] == arm]
                if m is not None:
                    ax.bar(index, m, color=COLORS[arm], alpha=.85, width=.66)
                    ax.text(index, m + highest * .035, f"{m:.2f}", ha="center", fontweight="bold", fontsize=12)
                    offsets = [(j - (len(values) - 1) / 2) * min(.05, .45 / max(1, len(values))) for j in range(len(values))]
                    ax.scatter([index + offset for offset in offsets], values,
                               s=25, color="#172C3B", edgecolor="white", linewidth=.5, zorder=3)
                if stats["failures"] or stats["missing"]:
                    ax.text(index, highest * .93, f"{stats['successes']}/{stats['planned']} passed",
                            ha="center", fontsize=9, color="#A1382E")
    stage = "CONFIRMATION" if data["gates"]["confirmation_eligible"] else data["stage"].upper()
    fig.text(.055, .965, f"Saved-route navigation time · {stage}", fontsize=22, fontweight="bold", va="top")
    fig.text(.055, .965 - .43 / fig.get_figheight(),
             f"{data['successes']}/{data['planned_trials']} trials passed · same apps and destinations · every successful run shown",
             fontsize=12, color="#52616E", va="top")
    note = "Bars = medians of successful runs. Dots = individual runs. Setup and the separate destination checker are excluded."
    if not data["gates"]["confirmation_eligible"]:
        note += "\nDevelopment pilot: these results do not establish reliability or a confirmed speedup."
    if data["profiled"]:
        note += "\nProfiling overhead is included; these times cannot support a speed claim."
    fig.text(.055, .04, note, fontsize=10, color="#52616E", va="bottom", linespacing=1.5)
    for suffix in ("png", "svg"):
        fig.savefig(output / f"replay-time.{suffix}", dpi=170,
                    metadata={"Creator": "Minimap replay benchmark", "Date": None} if suffix == "svg" else None)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    render(json.loads(args.summary.read_text()), args.output)


if __name__ == "__main__":
    main()
