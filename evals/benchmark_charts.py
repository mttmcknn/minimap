#!/usr/bin/env python3
"""Plot the controlled Compose benchmark from audited logs or a portable ledger.

No emulator or agent is launched. See requirements-benchmarks.txt for the
optional plotting/tokenization dependencies; evaluator tests stay stdlib-only.
"""
import argparse
from collections import Counter
import csv
from importlib.metadata import version
import json
from pathlib import Path
import platform
from statistics import median

from benchmark_data import ARMS, ENCODINGS, SAMPLES, collect, cumulative_cost, require, sha256, summarize

NAMES = {"jetsnack": "Jetsnack", "jetnews": "JetNews", "jetchat": "Jetchat"}
LABELS = {"raw": "Raw control", "new_graph": "First use", "reused_graph": "Graph reuse"}
COLORS = {"raw": "#3267A8", "new_graph": "#B96A20", "reused_graph": "#087F72"}
PURPLE = "#8462AB"
INK = "#182A3A"
MUTED = "#52616E"
CASES = {
    "renamed_control": "Renamed tab + repair",
    "relocated_route": "Relocated route + repair",
    "wrong_callback": "Wrong callback × 2",
    "wrong-person-1": "Wrong person · run 1",
    "wrong-person-2": "Wrong person · run 2",
    "wrong-person-3": "Wrong person · run 3",
    "external-navigation": "External navigation",
    "relabel": "Relabel + relearn",
}


def setup_plotting():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 12, "axes.titlesize": 14,
        "axes.labelsize": 12, "xtick.labelsize": 11, "ytick.labelsize": 11,
        "text.color": INK, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED,
        "axes.edgecolor": "#C8D0D7", "axes.spines.top": False, "axes.spines.right": False,
        "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
        "svg.fonttype": "none", "svg.hashsalt": "minimap-benchmark-v1",
    })
    return plt


def heading(fig, title, subtitle, footer):
    fig.text(.04, .965, title, fontsize=22, fontweight="bold", va="top")
    fig.text(.04, .919, subtitle, fontsize=12, color=MUTED, va="top")
    fig.text(.04, .025, footer, fontsize=10.5, color=MUTED, va="bottom", linespacing=1.5)


def save(fig, output, name, plt):
    fig.savefig(output / f"{name}.png", dpi=170)
    fig.savefig(output / f"{name}.svg", metadata={"Date": None, "Creator": "Minimap controlled benchmark"})
    plt.close(fig)


def paired_plot(data, output, plt):
    """Each dot is an actual matched difference, not a difference of medians."""
    fig, axes = plt.subplots(1, 2, figsize=(13, 6.8), sharey=True)
    fig.subplots_adjust(left=.21, right=.91, top=.80, bottom=.23, wspace=.40)
    groups = data["groups"]
    for ax, metric, color in zip(axes, ("o200k_base", "seconds"), (COLORS["reused_graph"], COLORS["new_graph"])):
        ax.axvline(0, color=INK, lw=1)
        ax.grid(axis="x", color="#E6EBEE", lw=.8)
        ax.set_axisbelow(True)
        for i, group in enumerate(groups):
            values = [p[metric] for p in group["paired"]]
            offsets = [(j - (len(values) - 1) / 2) * .07 for j in range(len(values))]
            ax.hlines(i, min(values), max(values), color=color, lw=1.7, alpha=.7)
            ax.scatter(values, [i + o for o in offsets], color=color, s=35, alpha=.65, zorder=3)
            m = median(values)
            ax.scatter([m], [i], marker="D", s=85, color=color, edgecolor="white", zorder=4)
            label = f"{m:.1f}%" if metric != "seconds" else f"{m:+.2f} s"
            ax.text(1.025, i, label, transform=ax.get_yaxis_transform(), fontsize=11, va="center", fontweight="bold")
        ax.set_ylim(len(groups) - .5, -.5)
    axes[0].set_yticks(range(len(groups)), [f"{NAMES[g['sample']]} · API {g['api']}" for g in groups])
    axes[0].set_xlim(0, 100)
    axes[0].set_xlabel("Tool-text token reduction (%)\nHigher means less text to read")
    axes[0].set_title("Graph reuse vs. raw control", loc="left", pad=16)
    deltas = [p["seconds"] for g in groups for p in g["paired"]]
    extent = max(abs(min(deltas)), abs(max(deltas))) * 1.15
    axes[1].set_xlim(-extent, extent)
    axes[1].set_xlabel("Reuse minus raw navigation time (s)\nPositive means slower")
    axes[1].set_title("Time tradeoff", loc="left", pad=16)
    heading(fig, "Less tool text; modest replay time overhead",
            "30 matched pairs · three Compose apps · two Android APIs · all 90 navigation trials passed",
            "Dots = five matched repeats per app/API; diamonds = paired medians; lines = observed min–max, not confidence intervals.\n"
            "Tokens: exact saved stdout + stderr under o200k_base, counted once. Model usage is unmeasured. Time excludes setup and grader.")
    save(fig, output, "01-paired-comparison", plt)


def trial_plot(data, output, plt, metric):
    from matplotlib.ticker import StrMethodFormatter
    fig, axes = plt.subplots(2, 3, figsize=(14, 9), sharex=True, sharey=True)
    fig.subplots_adjust(left=.08, right=.98, top=.82, bottom=.17, hspace=.39, wspace=.18)
    apis = sorted({g["api"] for g in data["groups"]})
    maximum = max(t[metric] for t in data["trials"])
    limit = maximum * 1.20
    for row, api in enumerate(apis):
        for col, sample in enumerate(SAMPLES):
            ax = axes[row, col]
            rows = [t for t in data["trials"] if t["api"] == api and t["sample"] == sample]
            repeats = sorted({t["repeat"] for t in rows})
            by_key = {(t["repeat"], t["arm"]): t for t in rows}
            for j, repeat in enumerate(repeats):
                offset = (j - (len(repeats) - 1) / 2) * .105
                xs = [i + offset for i in range(3)]
                ys = [by_key[repeat, arm][metric] for arm in ARMS]
                ax.plot(xs, ys, color="#9AA6B0", alpha=.28, lw=.7, zorder=1)
                for i, arm in enumerate(ARMS):
                    ax.scatter([xs[i]], [ys[i]], s=130, color=COLORS[arm], edgecolor="white", lw=.6, zorder=3)
                    ax.text(xs[i], ys[i], str(repeat), color="white", fontsize=7.5,
                            ha="center", va="center", zorder=4)
            for i, arm in enumerate(ARMS):
                values = [t[metric] for t in rows if t["arm"] == arm]
                m = median(values)
                ax.hlines(m, i - .3, i + .3, lw=2, color=COLORS[arm], zorder=2)
                label = f"{m:,.0f}" if metric != "seconds" else f"{m:.2f} s"
                ax.text(i, max(values) + limit * .055, label, color=COLORS[arm],
                        fontweight="bold", ha="center", fontsize=11)
            ax.set_title(f"{NAMES[sample]} · API {api}", loc="left", pad=12)
            ax.set_xticks(range(3), [LABELS[a] for a in ARMS])
            ax.tick_params(axis="x", labelbottom=True)
            ax.set_xlim(-.5, 2.5)
            ax.set_ylim(0, limit)
            ax.grid(axis="y", color="#E6EBEE", lw=.8)
            ax.set_axisbelow(True)
            ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
            if col == 0:
                ax.set_ylabel("Tool-text tokens (o200k_base)" if metric != "seconds" else "Navigation tool time (s)")
    is_tokens = metric != "seconds"
    heading(fig, "Every trial: saved tool-response tokens" if is_tokens else "Every trial: navigation time",
            "90 measured trials · five numbered repeats per arm · labels and horizontal ticks show arm medians · shared vertical scale",
            "Numbers 1–5 identify matched repeat blocks; faint lines connect matching blocks, not chronological action sequences.\n"
            + ("Exact tokenization of each saved stdout/stderr string, summed once. Excludes prompts, reasoning, skill loading, setup and grader."
               if is_tokens else "First use includes initialization, labels and recording. Replay uses a copied graph. Setup/readiness and grader time are excluded."))
    save(fig, output, "02-tool-tokens" if is_tokens else "03-navigation-time", plt)


def learning_plot(data, output, plt):
    from matplotlib.lines import Line2D
    from matplotlib.ticker import StrMethodFormatter
    fig, axes = plt.subplots(2, 3, figsize=(14, 9.5), sharex=True, sharey=True)
    fig.subplots_adjust(left=.08, right=.98, top=.77, bottom=.18, hspace=.44, wspace=.18)
    skill = data["skill"]["tokens"]["o200k_base"]
    uses = list(range(1, 11))
    for group, ax in zip(data["groups"], axes.flat):
        raw, first, reuse = (group["arms"][a]["o200k_base"] for a in ARMS)
        baseline = [n * raw for n in uses]
        once = [cumulative_cost(n, raw, first, reuse, upfront=skill)[1] for n in uses]
        every = [cumulative_cost(n, raw, first, reuse, per_use=skill)[1] for n in uses]
        ax.plot(uses, baseline, color=COLORS["raw"], lw=2.4)
        ax.plot(uses, once, color=COLORS["reused_graph"], lw=2.4)
        ax.plot(uses, every, color=PURPLE, lw=2.0, linestyle="--")
        projections = group["projections"]["o200k_base"]
        be = projections["skill_once"]["break_even_total_uses"]
        recurring = projections["skill_every_use"]["break_even_total_uses"]
        ax.scatter([be], [once[be - 1]], color=COLORS["reused_graph"], s=50, zorder=4)
        ax.text(.04, .94, f"Break-even: {be} uses if skill loads once\n"
                + (f"{recurring} uses if skill loads every use" if recurring else "No break-even if skill loads every use"),
                transform=ax.transAxes, va="top", fontsize=10, linespacing=1.55,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": .85, "pad": 1})
        ax.set_title(f"{NAMES[group['sample']]} · API {group['api']}", loc="left", pad=12)
        ax.set_xlim(1, 10)
        ax.set_xticks([1, 2, 4, 6, 8, 10])
        ax.tick_params(axis="x", labelbottom=True)
        ax.set_ylim(0, 25000)
        ax.set_yticks([0, 5000, 10000, 15000, 20000, 25000])
        ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
        ax.grid(color="#E6EBEE", lw=.8)
        ax.set_axisbelow(True)
        ax.set_xlabel("Total uses of the same route")
    for ax in axes[:, 0]:
        ax.set_ylabel("Cumulative text tokens (o200k_base)")
    fig.legend(handles=[Line2D([0], [0], color=COLORS["raw"], lw=2.4, label="Raw control"),
                        Line2D([0], [0], color=COLORS["reused_graph"], lw=2.4, label="Minimap + skill loaded once"),
                        Line2D([0], [0], color=PURPLE, lw=2.0, linestyle="--", label="Minimap + skill loaded every use")],
               loc="upper center", bbox_to_anchor=(.52, .864), ncol=3, frameon=False, fontsize=11)
    heading(fig, "Skill loading changes the projected savings",
            f"Projection from measured medians, not an observed 10-task run · one learning pass · frozen navigation skill = {skill:,} tokens",
            f"Raw(N) = N × raw. Minimap(N) = first use + (N − 1) × reuse + skill loads × {skill:,}. Break-even includes the first use.\n"
            "Each text is counted once per load. Model context replay, caching, reasoning, exploration and repairs are unmeasured; these are not billing curves.")
    save(fig, output, "04-learning-cost", plt)


def controls_plot(data, output, plt):
    from matplotlib.patches import Patch
    fig, axes = plt.subplots(1, 2, figsize=(14, 8.5), sharey=True)
    fig.subplots_adjust(left=.25, right=.93, top=.82, bottom=.19, wspace=.3)
    colors = {"36": COLORS["raw"], "37": PURPLE}
    for ax, metric in zip(axes, ("seconds", "o200k_base")):
        for i, case in enumerate(CASES):
            for api, offset in (("36", -.17), ("37", .17)):
                row = next(c for c in data["controls"] if c["api"] == api and c["case"] == case)
                value = row[metric]
                ax.barh(i + offset, value, height=.28, color=colors[api], alpha=.9)
                label = f"{value:.1f}" if metric == "seconds" else f"{value:,}"
                if not row["passed"]:
                    label += " FAIL"
                ax.annotate(label, (value, i + offset), xytext=(5, 0), textcoords="offset points", va="center", fontsize=10)
        ax.set_xlim(0, max(c[metric] for c in data["controls"]) * 1.21)
        ax.grid(axis="x", color="#E6EBEE")
        ax.set_axisbelow(True)
        ax.set_ylim(len(CASES) - .5, -.5)
    axes[0].set_yticks(range(len(CASES)), CASES.values())
    axes[0].set_xlabel("Scripted case tool time (s)")
    axes[1].set_xlabel("Saved tool-text tokens (o200k_base)")
    fig.legend(handles=[Patch(color=color, label=f"API {api}") for api, color in colors.items()],
               frameon=False, ncol=2, loc="upper right", bbox_to_anchor=(.95, .905), fontsize=11)
    passed = sum(c["passed"] for c in data["controls"])
    heading(fig, "Recovery and rejection controls: each recorded case",
            f"{passed}/{len(data['controls'])} scripted cases passed · one run per case/API · different scenarios perform different amounts of work",
            "Renamed/relocated cases include stale replay, repair, teammate replay and older-build checks; setup and independent grader are excluded.\n"
            "Wrong-person cases include reaching the profile and rejecting the wrong goal; relabel excludes teammate replay. These are not agent repair latencies.")
    save(fig, output, "05-controls", plt)


def outcomes_plot(data, output, plt):
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch
    colors = {"passed": "#97CEB4", "setup_failed": "#EEC184", "failed": "#CF7C80", "missing": "#E5E9ED"}
    state_ids = {name: i for i, name in enumerate(colors)}
    letters = {"passed": "P", "setup_failed": "S", "failed": "F", "missing": "–"}
    labels = [(g["api"], g["sample"], arm) for g in data["groups"] for arm in ARMS]
    fig, axes = plt.subplots(1, 2, figsize=(12, 10.8), sharey=True)
    fig.subplots_adjust(left=.29, right=.96, top=.81, bottom=.19, wspace=.15)
    for ax, cohort in zip(axes, ("Original startup", "Readiness wait")):
        rows = [r for r in data["quality"] if r["cohort"] == cohort]
        cells = {(r["api"], r["sample"], r["arm"], r["repeat"]): r["state"] for r in rows}
        matrix = [[state_ids[cells[(*key, repeat)]] for repeat in range(1, 6)] for key in labels]
        ax.imshow(matrix, cmap=ListedColormap(list(colors.values())), vmin=0, vmax=3, aspect="auto", interpolation="nearest")
        for i, key in enumerate(labels):
            for j in range(5):
                ax.text(j, i, letters[cells[(*key, j + 1)]], ha="center", va="center", fontsize=11)
        for i in range(0, len(labels), 3):
            ax.axhline(i - .5, color="white", lw=3)
        counts = Counter(r["state"] for r in rows)
        ax.set_title(f"{cohort}\n{counts['passed']}/{len(rows)} planned trials passed", loc="left", fontsize=13, pad=13)
        ax.set_xticks(range(5), range(1, 6))
        ax.set_xlabel("Matched repeat")
        ax.tick_params(axis="both", length=0, pad=8)
        for spine in ax.spines.values():
            spine.set_visible(False)
    axes[0].set_yticks(range(len(labels)), [f"{NAMES[sample]} · {api} · {LABELS[arm]}" for api, sample, arm in labels], fontsize=10.5)
    fig.legend(handles=[Patch(color=colors[key], label=label) for key, label in (
        ("passed", "P: passed"), ("setup_failed", "S: setup failed"), ("failed", "F: measured failure"), ("missing", "–: not run"))],
        loc="lower center", bbox_to_anchor=(.53, .085), frameon=False, ncol=4, fontsize=11)
    heading(fig, "All planned outcomes, including the failed startup cohort",
            "Separate cohorts, same product binary · the follow-up added a bounded Home-readiness wait before measurement",
            "Original: 52 passed, 8 setup failures, 30 not run. Follow-up: 90 passed. No trial is removed from either planned denominator.\n"
            "Two earlier activity-launch preflights failed before trial assignment (0 trials each); preserved in the source report, outside these grids.")
    save(fig, output, "06-outcomes", plt)


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def markdown(data):
    groups = data["groups"]
    reductions = [median(p["o200k_base"] for p in g["paired"]) for g in groups]
    times = [median(p["seconds"] for p in g["paired"]) for g in groups]
    skill = data["skill"]["tokens"]["o200k_base"]
    lines = [
        "# Minimap benchmark graphs — September 17, 2026", "",
        f"Graph reuse returned **{min(reductions):.1f}–{max(reductions):.1f}% fewer tool-text tokens**, "
        f"with **{min(times):.2f}–{max(times):.2f} seconds more navigation time** at the paired median in each app/API case. "
        "All 90 primary trials passed their independent destination check. "
        "These are measurements of recorded tool output and subprocess time; **actual model token usage and billed savings remain unmeasured**.", "",
        "![Matched token and timing effects](01-paired-comparison.png)", "",
        "## What was compared", "",
        "Three pinned Jetpack Compose samples, Android APIs 36 and 37, three arms, and five matched repeat blocks: "
        "raw Android navigation with a known route; Minimap first use including initialization, labels, and recording; "
        "and Minimap replay from a copied graph with no runtime cache. "
        "The routes are Jetsnack Home → Search, JetNews Home → Interests, and Jetchat conversation → Ali Conors profile. "
        "This is a scripted device benchmark; the agent discovery and diagnosis experiment has not run.", "",
        "Each saved UTF-8 stdout and stderr string was tokenized independently, preserving whitespace, then summed once per measured phase. "
        "`o200k_base` is the primary reference encoding and `cl100k_base` is a sensitivity check. "
        "No encoding is assumed to match a particular current agent model. "
        "[OpenAI's tokenizer documentation](https://developers.openai.com/cookbook/examples/how_to_count_tokens_with_tiktoken) "
        "describes counting a string under a named encoding; complete model usage also depends on messages and tools. "
        "These counts exclude prompts, tool schemas/wrappers, source reads, reasoning, context replay, caching and the independent evaluator. "
        "Tool-response text would generally become model input, not generated model output.", "",
        "Navigation seconds are sums of measured top-level subprocess durations, excluding fixture setup/readiness and the independent destination grader. "
        "Those costs and their tokens remain in `trials.csv`; `total_seconds_including_setup_oracle` preserves the recorded complete trial duration. "
        "No setup failure is treated as a fast zero-second navigation. Both emulators shared one host. "
        "With five blocks per case, charts show all observations and their range, not p95 or population confidence claims.", "",
        "## Every navigation trial", "",
        "Every numbered dot is one trial; its number identifies a matched repeat block, not execution order. "
        "Horizontal ticks and numeric labels show arm medians. Both figures use shared vertical scales across all six cases.", "",
        "![All 90 token measurements](02-tool-tokens.png)", "",
        "| App | API | Raw tokens | First-use tokens | Reuse tokens | Paired token reduction | Raw seconds | First-use seconds | Reuse seconds | Paired extra seconds |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for g in groups:
        token_values = [g["arms"][a]["o200k_base"] for a in ARMS]
        seconds = [g["arms"][a]["seconds"] for a in ARMS]
        lines.append(f"| {NAMES[g['sample']]} | {g['api']} | " + " | ".join(f"{v:,.0f}" for v in token_values)
                     + f" | {median(p['o200k_base'] for p in g['paired']):.1f}% | " + " | ".join(f"{v:.3f}" for v in seconds)
                     + f" | {median(p['seconds'] for p in g['paired']):+.3f} |")
    lines.extend([
        "", "Paired effects are calculated within each repeat before taking the median; they are not differences of unrelated arm medians.", "",
        "![All 90 timing measurements](03-navigation-time.png)", "",
        "First-use tool time is higher because it includes recording. First-use tool text is already smaller than raw output in this known-route fixture; "
        "that does **not** establish that an agent can discover or teach an unseen route more cheaply. "
        "Replay performs destination verification internally, returning one compact response. The raw control also checks the destination. "
        "There is no demonstrated time break-even against this scripted raw control: replay's median time is higher in all six cases.", "",
        "## Learning-cost math and instruction overhead", "",
        "Let `R` be raw tokens per task, `L` first-use tokens, `G` reused-graph tokens, `N` total route uses, "
        "`H` skill tokens, `S` skill loads, and `E` any additional text from exploration or maintenance:", "",
        "```text\nRaw(N)     = N × R\nMinimap(N) = L + (N − 1) × G + S × H + E\nSaved(N)   = Raw(N) − Minimap(N)\n```", "",
        f"The full frozen navigation skill contains **{skill:,} `o200k_base` tokens** "
        f"({data['skill']['tokens']['cl100k_base']:,} under `cl100k_base`). "
        "The chart uses measured arm medians and explicitly assumes `E = 0`. "
        "It compares loading the skill once (`S = 1`) with loading it for every task (`S = N`). "
        "Only the Minimap skill is added; raw-tool instruction overhead is unmeasured and assigned zero in these scenarios. "
        "The first task is included in `L`; no extra first replay is charged. "
        "Break-even is the first integer use where Minimap is no more expensive and stays so. "
        "These are projections of text volume counted once per load, not measured long-running agent sessions or billing forecasts. "
        "Actual model calls can repeatedly consume context; caching and compaction change that cost.", "",
        "![Projected cumulative text and skill overhead](04-learning-cost.png)", "",
        "| App | API | Break-even: tool text only | Break-even: skill once | Break-even: skill every use | Raw tokens at 10 | Minimap at 10, skill once | Savings at 10, skill once | Savings at 10, skill every use |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for g in groups:
        p = g["projections"]["o200k_base"]
        be = [p[k]["break_even_total_uses"] for k in ("payload_only", "skill_once", "skill_every_use")]
        once, every = p["skill_once"], p["skill_every_use"]
        lines.append(f"| {NAMES[g['sample']]} | {g['api']} | " + " | ".join(str(v) if v is not None else "none" for v in be)
                     + f" | {once['raw_at_10']:,} | {once['minimap_at_10']:,} | {once['savings_percent_at_10']:.1f}% | {every['savings_percent_at_10']:+.1f}% |")
    example = next(g for g in groups if g["sample"] == "jetchat" and g["api"] == "36")
    r, first, reuse = (example["arms"][a]["o200k_base"] for a in ARMS)
    scenario = example["projections"]["o200k_base"]["skill_once"]
    lines.extend([
        "", f"For example, Jetchat/API 36 over ten uses gives `10 × {r:,} = {10 * r:,}` raw tokens, "
        f"versus `{first:,} + 9 × {reuse:,} + {skill:,} = {scenario['minimap_at_10']:,}` with one skill load: "
        f"**{scenario['savings_at_10']:,} fewer text tokens ({scenario['savings_percent_at_10']:.1f}%)**. "
        "Reloading the entire skill each task erases the projected advantage for JetNews and Jetchat. "
        "This makes concise instructions and amortizing skill/context overhead important design targets. "
        "Graph JSON is read locally by the CLI; loading it into the model is not assumed. "
        "Discovery and repair costs, their frequency, and actual context behavior still need agent-level measurements.", "",
        "## Tokenizer sensitivity", "",
        "The result is similar under a second encoding; that is a robustness check on text volume, not a claim about every model.", "",
        "| App | API | o200k raw → reuse | o200k paired reduction | cl100k raw → reuse | cl100k paired reduction |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for g in groups:
        cells = []
        for enc in ENCODINGS:
            cells.extend([f"{g['arms']['raw'][enc]:,} → {g['arms']['reused_graph'][enc]:,}",
                          f"{median(p[enc] for p in g['paired']):.2f}%"])
        lines.append(f"| {NAMES[g['sample']]} | {g['api']} | " + " | ".join(cells) + " |")
    lines.extend([
        "", "## Recovery and negative controls", "",
        "These source-informed scripts test the recovery contract. Renamed and relocated routes must fail stale replay, learn a repair, "
        "work from a teammate graph copy, and remain compatible with the old build. Wrong callbacks must reproduce without corrupting the graph. "
        "Wrong-person checks must reject the goal; external navigation must not produce a false success; relabel/relearn must preserve IDs. "
        "Each API has eight case records, including all three wrong-person checks.", "",
        "![Individual control case costs](05-controls.png)", "",
        "Repair rows include multiple operations (stale replay, repair, teammate and old-build checks). "
        "Relabel costs exclude the separate teammate replay; wrong-person costs include reaching the profile before rejection. "
        "Fixture setup and independent oracles are outside these phase totals. "
        "These are case costs with different scopes, not single-repair latency comparisons. "
        "They do not measure an agent diagnosing an unexpected change.", "",
        "| Case | API | Outcome | Tool seconds | Calls | Tool-text tokens |",
        "| --- | ---: | --- | ---: | ---: | ---: |",
    ])
    for case in CASES:
        for c in (c for c in data["controls"] if c["case"] == case):
            lines.append(f"| {CASES[case]} | {c['api']} | {'pass' if c['passed'] else 'FAIL'} | {c['seconds']:.3f} | {c['commands']} | {c['o200k_base']:,} |")
    audit = data["graph_audit"]["checks"]
    graph_rows = [r for r in audit if r["graph_present"]]
    unchanged = [r for r in graph_rows if r.get("replay_graph_unchanged")]
    lines.extend([
        "", f"The separate graph audit passed {sum(r['passed'] for r in audit)}/{len(audit)} assignments: "
        f"{len(graph_rows)} learned/copied graphs were valid and doctor stayed read-only; "
        f"{len(unchanged)} reused graphs were unchanged. "
        f"Real-Git disjoint additions: {'pass' if data['team_git']['disjoint_additions']['passed'] else 'FAIL'}; "
        f"conflicting-edit rejection: {'pass' if data['team_git']['conflicting_edits']['passed'] else 'FAIL'}. "
        "Git checks ran once on the host and have no comparable device-time measurement.", "",
        "## Outcomes and retained failures", "",
        "![Every planned outcome in both cohorts](06-outcomes.png)", "",
        "The earlier startup cohort had 52 successes, 8 setup failures, and 30 missing assignments out of 90 planned. "
        "The follow-up retained the product binary and added a bounded Home-readiness wait to fixture setup; it reached 90/90. "
        "They are separate experiments, not retries substituted into one denominator. "
        "Two earlier activity-launch preflights failed before assigning any trials. "
        "The original reports are unchanged and remain linked in the [controlled report](../2026-09-16-controlled.md).", "",
        "## Reproduce and inspect", "",
        "- [Portable analysis ledger](benchmark.json): all 90 primary records, 16 controls, paired effects, projections, source hashes, and 1,146 call records.",
        "- [Every primary trial](trials.csv), [every audited call](calls.csv), [control cases](controls.csv), and [all planned outcomes](outcomes.csv).",
        "- Each PNG has a same-named SVG for vector export; [artifact checksums](SHA256SUMS) cover the deliverables.",
        "- [Analysis and chart script](../../benchmark_charts.py), [audit and cost math](../../benchmark_data.py), [pinned optional dependencies](../../requirements-benchmarks.txt).", "",
        "From the repository root, rebuild charts and tables from the checked-in ledger without emulators, agents, network access, or temporary raw logs "
        "(after installing the optional dependencies):", "",
        "```sh\npython3 -m venv /tmp/minimap-chart-env\n/tmp/minimap-chart-env/bin/pip install -r evals/requirements-benchmarks.txt\n"
        "/tmp/minimap-chart-env/bin/python evals/benchmark_charts.py \\\n  --data evals/results/2026-09-17-benchmarks/benchmark.json \\\n  --output /tmp/minimap-charts-rebuilt\n```", "",
        "To re-audit/tokenize the original saved strings, preserve the complete raw artifact directory and run:", "",
        "```sh\n/tmp/minimap-chart-env/bin/python evals/benchmark_charts.py \\\n  --report evals/results/2026-09-16-controlled.json \\\n  --raw-root /absolute/path/to/minimap-controlled-20260916 \\\n  --output /tmp/minimap-charts-retokenized\n```", "",
        "Extraction verifies source report SHA-256 values, every saved stream's byte count, unique assignments, and per-phase command/time/byte totals. "
        "The CSV/JSON preserve per-stream SHA-256 values and token counts. Re-rendering a ledger does not re-audit missing raw files. "
        "Raw strings remain in the external artifact directory rather than being duplicated into the repository. "
        "The output directory must be new; the tool does not overwrite historical reports.", "",
        f"Frozen binary: `{data['binary_sha256']}`. Compose revision: `{data['compose_samples_revision']}`. "
        f"Controlled report SHA-256: `{data['controlled_report_sha256']}`. "
        f"Tokenizer environment: `{json.dumps(data['tokenization']['versions'], sort_keys=True)}`. "
        f"Skill fragment SHA-256: `{data['skill']['sha256']}`.", "",
        "## Remaining measurement gaps", "",
        *[f"- {limit}" for limit in data["limits"]], "",
        f"The prepared agent layer has **{data['agent_layer']['started']}/{data['agent_layer']['planned']} trials started**. "
        "Actual input, cached input, output and reasoning usage, autonomous discovery/repair success, and user interruption rates are still unmeasured. "
        "The next agent experiment should report those alongside task success, end-to-end time, skill/context overhead and recurring maintenance cost. "
        "No new agent or emulator trials were run to produce these charts.", "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", type=Path, help="Controlled report; audit and tokenize its external raw logs")
    source.add_argument("--data", type=Path, help="Portable benchmark.json; re-render without raw logs or a tokenizer")
    parser.add_argument("--raw-root", type=Path, help="Relocated raw artifact root; only with --report")
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    args = parser.parse_args()
    require(not args.output.exists(), "Output directory already exists; preserve historical benchmark artifacts")
    require(args.report is not None or args.raw_root is None, "--raw-root requires --report")
    if args.report:
        import tiktoken
        data = collect(args.report, args.raw_root, {n: tiktoken.get_encoding(n) for n in ENCODINGS},
                       {"python": platform.python_version(), "tiktoken": version("tiktoken")})
    else:
        data = json.loads(args.data.read_text())
        require(data["schema_version"] == 1, "Unsupported benchmark schema")
        require(data["groups"] == summarize(data["trials"], data["skill"]["tokens"]), "Ledger summaries do not match its trials")
    require(len(data["groups"]) == 6 and {g["api"] for g in data["groups"]} == {"36", "37"}
            and all(g["repeats"] == [1, 2, 3, 4, 5] for g in data["groups"]),
            "This chart layout targets the six controlled Compose cases with five matched blocks each")
    plt = setup_plotting()
    args.output.mkdir(parents=True)
    (args.output / "benchmark.json").write_text(json.dumps(data, indent=2) + "\n")
    for name, rows in (("trials", data["trials"]), ("calls", data["calls"]), ("controls", data["controls"]), ("outcomes", data["quality"])):
        write_csv(args.output / f"{name}.csv", rows)
    paired_plot(data, args.output, plt)
    trial_plot(data, args.output, plt, "o200k_base")
    trial_plot(data, args.output, plt, "seconds")
    learning_plot(data, args.output, plt)
    controls_plot(data, args.output, plt)
    outcomes_plot(data, args.output, plt)
    (args.output / "README.md").write_text(markdown(data))
    (args.output / "render-environment.json").write_text(json.dumps({
        "python": platform.python_version(), "matplotlib": version("matplotlib"), "numpy": version("numpy"),
        "chart_script_sha256": sha256(Path(__file__).read_bytes()),
        "data_script_sha256": sha256(Path(__file__).with_name("benchmark_data.py").read_bytes()),
    }, indent=2) + "\n")
    (args.output / "SHA256SUMS").write_text("".join(f"{sha256(p.read_bytes())}  {p.name}\n" for p in sorted(args.output.iterdir()) if p.is_file()))
    print(json.dumps({"output": str(args.output.resolve()), "trials": len(data["trials"]), "controls": len(data["controls"]),
                      "audited_calls": len(data["calls"]), "model_usage": data["tokenization"]["model_usage"]}))


if __name__ == "__main__":
    main()
