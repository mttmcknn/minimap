#!/usr/bin/env python3
"""Render every replay time from an analyzed cohort; no device or agent work."""
import argparse
import json
from pathlib import Path
from statistics import median

from analyze_replay import ARMS, successful
from benchmark_charts import NAMES, setup_plotting

COLORS = {"raw": "#37659B", "baseline": "#B16B31", "candidate": "#087F72"}
LABELS = ("Without\nMinimap", "Released\nMinimap", "Candidate")


def save_figure(fig, output, name):
    for suffix in ("png", "svg"):
        fig.savefig(output / f"{name}.{suffix}", dpi=170,
                    metadata={"Creator": "Minimap replay benchmark", "Date": None} if suffix == "svg" else None)


def render(data, output, medians_only=False):
    plt = setup_plotting()
    apis = sorted({group["api"] for group in data["groups"]})
    samples = ["jetsnack", "jetnews", "jetchat"]
    fig, axes = plt.subplots(len(apis), 3, figsize=(12, 3.7 * len(apis) + 2.8),
                             squeeze=False, sharey=True)
    fig.subplots_adjust(top=1 - 1.35 / fig.get_figheight(), bottom=1.9 / fig.get_figheight(),
                        left=.07, right=.98, hspace=.55, wspace=.22)
    rows = [row for row in data["trials"] if successful(row)]
    plotted = ([arm["median_successful_seconds"] for group in data["groups"]
                for arm in group["arms"].values() if arm["median_successful_seconds"] is not None]
               if medians_only else [row["seconds"] for row in rows])
    highest = max(plotted, default=1) * 1.2
    for y, api in enumerate(apis):
        for x, sample in enumerate(samples):
            ax = axes[y, x]
            group = next((g for g in data["groups"] if g["api"] == api and g["sample"] == sample), None)
            ax.set_title(f"{NAMES[sample]} · API {api}", loc="left", fontweight="bold")
            labels = ("Without\nMinimap", "Previous\nMinimap", "New\nMinimap") if medians_only else LABELS
            ax.set_xticks(range(3), labels)
            ax.set_xlim(-.5, 2.5)
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
                    value = f"{m:.1f}" if medians_only else f"{m:.2f}"
                    label_y = m + highest * .035 if medians_only else min(m * .5, highest * .08)
                    ax.text(index, label_y, value, ha="center", fontweight="bold", fontsize=12,
                            color="#172C3B" if medians_only else "white")
                    if not medians_only:
                        offsets = [(j - (len(values) - 1) / 2) * min(.05, .45 / max(1, len(values))) for j in range(len(values))]
                        ax.scatter([index + offset for offset in offsets], values,
                                   s=25, color="#172C3B", edgecolor="white", linewidth=.5, zorder=3)
                if stats["failures"] or stats["missing"]:
                    ax.text(index, highest * .93, f"{stats['successes']}/{stats['planned']} passed",
                            ha="center", fontsize=9, color="#A1382E")
    stage = "CONFIRMATION" if data["gates"]["confirmation_eligible"] else data["stage"].upper()
    fig.text(.055, .965, f"Saved-route navigation time · {stage}", fontsize=22, fontweight="bold", va="top")
    detail = "typical time (median)" if medians_only else "every successful run shown"
    fig.text(.055, .965 - .43 / fig.get_figheight(),
             f"{data['successes']}/{data['planned_trials']} trials passed · same apps and destinations · {detail}",
             fontsize=12, color="#52616E", va="top")
    note = ("Bars = medians of successful runs. Previous = v0.2.0; new = candidate. All runs: replay-time.png."
            if medians_only else "Bars = medians of successful runs. Dots = individual runs.")
    note += "\nSetup and the separate destination checker are excluded."
    if data["stage"] != "confirmation":
        note += "\nDevelopment run: these results do not establish reliability or a confirmed speedup."
    elif not data["gates"]["confirmation_eligible"] or not data["gates"]["complete_assignments"]:
        note += "\nConfirmation is incomplete; these results cannot support a speed claim."
    elif not data["gates"]["speed_goal_met"]:
        note += "\nSpeed target NOT met: correctness or timing checks failed."
    if data["profiled"]:
        note += "\nProfiling overhead is included; these times cannot support a speed claim."
    fig.text(.055, .04, note, fontsize=10, color="#52616E", va="bottom", linespacing=1.5)
    save_figure(fig, output, "replay-summary" if medians_only else "replay-time")
    plt.close(fig)


def render_pairs(data, output):
    """Show each verified within-block comparison, including slower replays."""
    plt = setup_plotting()
    sample_order = {name: index for index, name in enumerate(("jetsnack", "jetnews", "jetchat"))}
    groups = sorted(data["groups"], key=lambda group: (group["api"], sample_order[group["sample"]]))
    fig, ax = plt.subplots(figsize=(12, 8.5))
    fig.subplots_adjust(left=.20, right=.96, top=.80, bottom=.24)
    values = [-pair["seconds"] for group in groups for comparison in group["paired"].values()
              for pair in comparison["pairs"]]
    bound = max([abs(value) for value in values], default=1) * 1.18 or 1
    ax.set_xlim(-bound, bound)
    ax.set_ylim(len(groups) - .5, -.5)
    ax.axvline(0, color="#52616E", linewidth=1.2)
    ax.grid(axis="x", color="#E6EBEE")
    ax.set_axisbelow(True)
    controls = (("raw", -.15, "Compared with no Minimap"),
                ("baseline", .15, "Compared with released Minimap"))
    for row, group in enumerate(groups):
        for control, offset, label in controls:
            paired = group["paired"][control]
            seconds = [-pair["seconds"] for pair in paired["pairs"]]
            # Small fixed vertical offsets keep near-equal observations visible.
            spread = [(i - (len(seconds) - 1) / 2) * .015 for i in range(len(seconds))]
            ax.scatter(seconds, [row + offset + gap for gap in spread], s=35,
                       color=COLORS[control], alpha=.7, edgecolor="white", linewidth=.4,
                       label=label if row == 0 else None)
            if paired["median_seconds"] is not None:
                ax.scatter(-paired["median_seconds"], row + offset, marker="D", s=80,
                           color=COLORS[control], edgecolor="#172C3B", linewidth=1.2, zorder=4)
    labels = [f"{NAMES[group['sample']]} · API {group['api']}\n"
              f"{group['paired']['raw']['blocks']} / {group['paired']['baseline']['blocks']} verified pairs"
              for group in groups]
    ax.set_yticks(range(len(groups)), labels, fontsize=10)
    ax.set_xlabel("Seconds saved by the new Minimap · negative means it took longer", labelpad=12)
    ax.legend(loc="upper center", bbox_to_anchor=(.5, 1.14), ncol=2, frameon=False)
    fig.text(.055, .955, "Time saved on each matched trip", fontsize=22, fontweight="bold", va="top")
    fig.text(.055, .901, "Each dot is one comparison. Diamonds show the middle result (median).",
             fontsize=12, color="#52616E", va="top")
    planned = sum(group["arms"]["candidate"]["planned"] * 2 for group in groups)
    note = (f"{len(values)}/{planned} paired comparisons verified. Row counts: no Minimap / released Minimap."
            "\nBoth trips must pass to form a pair; failures stay in the full trial data and pass counts."
            "\nSetup and the separate destination checker are excluded from these times.")
    if not data["gates"]["speed_goal_met"]:
        note += "\nThe full speed-and-correctness goal has not passed. These are descriptive comparisons."
    fig.text(.055, .045, note, fontsize=10, color="#52616E", va="bottom", linespacing=1.5)
    save_figure(fig, output, "replay-pairs")
    plt.close(fig)


def render_text(data, output):
    """Plot exact tool-output counts, without treating them as billed usage."""
    plt = setup_plotting()
    samples = ("jetsnack", "jetnews", "jetchat")
    fig, axes = plt.subplots(1, 3, figsize=(12, 6), sharey=True)
    fig.subplots_adjust(left=.075, right=.98, top=.74, bottom=.32, wspace=.25)
    rows = [row for row in data["trials"] if row["success"] and row["arm"] in ("raw", "candidate")]
    highest = max((row["o200k_base"] for row in rows), default=1) * 1.2
    for ax, sample in zip(axes, samples):
        ax.set_title(NAMES[sample], loc="left", fontweight="bold")
        ax.set_xticks((0, 1), ("Without\nMinimap", "New\nMinimap"))
        ax.set_xlim(-.5, 1.5)
        ax.set_ylim(0, highest)
        ax.grid(axis="y", color="#E6EBEE")
        ax.set_axisbelow(True)
        for index, arm in enumerate(("raw", "candidate")):
            assigned = [row for row in data["trials"] if row["sample"] == sample and row["arm"] == arm]
            values = [row["o200k_base"] for row in assigned if row["success"]]
            if values:
                value = median(values)
                ax.bar(index, value, color=COLORS[arm], width=.60)
                ax.text(index, value + highest * .035, f"{value:,.0f}", ha="center", fontweight="bold", fontsize=13)
            ax.text(index, -.30, f"{len(values)}/{len(assigned)} verified", ha="center",
                    fontsize=9, color="#52616E", transform=ax.get_xaxis_transform())
    axes[0].set_ylabel("Text returned by tools · tokens")
    fig.text(.055, .955, "Less text for the AI to read", fontsize=22, fontweight="bold", va="top")
    fig.text(.055, .875, "A token is a small chunk of text. Shorter bars mean less tool output.",
             fontsize=12, color="#52616E", va="top")
    fig.text(.055, .045,
             "Bars = medians of successful trips, combining both Android versions (o200k_base)."
             "\nOnly navigation stdout and stderr are counted, once each. Instructions and AI reasoning are excluded."
             "\nThis is not a measurement of total model tokens or money saved.",
             fontsize=10, color="#52616E", va="bottom", linespacing=1.5)
    save_figure(fig, output, "replay-text")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--medians-only", action="store_true", help="Readable overview; full chart retains every run and outlier")
    view.add_argument("--paired", action="store_true", help="Show every verified paired time difference and its median")
    view.add_argument("--text-tokens", action="store_true", help="Read a replay_text.py audit and graph tool-output tokens")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    data = json.loads(args.summary.read_text())
    if args.text_tokens:
        render_text(data, args.output)
    elif args.paired:
        render_pairs(data, args.output)
    else:
        render(data, args.output, args.medians_only)


if __name__ == "__main__":
    main()
