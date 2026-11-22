#!/usr/bin/env python3
"""
analyze.py - Performance evaluation: compares static routing (baseline) with the
SDN approaches under several conditions, using the logs in results/.

    python3 analyze.py

Reads                                   Writes (results/)
  results/congestion/<mode>/             comparison.md               all tables
  results/linkfail/<mode>/               congestion_throughput.png   per-flow throughput
  results/sweep/<mode>_r<rate>/          congestion_paths.png        load on each path
  results/flows/<mode>_n<flows>/         congestion_summary.png      loss + delay per flow
                                         linkfail_throughput.png     link failure
                                         linkfail_packets.png        sequence numbers per packet
                                         sweep.png                   loss/delay vs. load
                                         flows.png                   increasing number of flows

Metric definitions
  throughput     UDP payload Mbps received by the server (0.5 s intervals)
  loss %         lost / sent, lost = gaps in sequence numbers
  delay          one-way delay (receive time - send time; Mininet hosts share a clock)
  gap            a jump in the sequence numbers = packets that never arrived
  silence        longest time between two received packets of a flow
  fairness       Jain's index of (throughput / requested rate) over all flows, 1 = fair
  detection      start of the problem -> controller logs CONGESTION_DETECTED
  reroute        start of the problem -> controller logs REROUTED
  recovery       start of the problem -> end of the first 0.5 s interval after which
                 every active flow has < 1 % loss and >= 90 % of its rate for 1 s
                 (upper bound, resolution 0.5 s)
"""

import csv
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
MODES = ["static", "adaptive", "app_aware"]
MODE_LABEL = {"static": "Static (baseline)", "adaptive": "Adaptive (switch stats)",
              "app_aware": "App-aware (proposed)"}
MODE_COLORS = {"static": "#868580", "adaptive": "#6250d6", "app_aware": "#008300"}
FLOW_COLORS = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a"}
PATH_COLORS = {"P1": "#2a78d6", "P2": "#eb6834", "P3": "#1baf7a"}
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"


# =================================================================== loading
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def load_run(d):
    meta_path = os.path.join(d, "scenario.json")
    if not os.path.exists(meta_path):
        return None
    with open(meta_path) as fh:
        meta = json.load(fh)
    t0 = meta["t0"]
    run = {"dir": d, "meta": meta, "t0": t0, "flows": {}, "events": [], "links": {}}
    for f in meta["flows"]:
        rows = []
        for r in read_csv(os.path.join(d, "flow_%s.csv" % f["name"])):
            rows.append({"t": float(r["time"]) - t0, "mbps": float(r["throughput_mbps"]),
                         "pkts": int(r["packets"]), "lost": int(r["lost"]),
                         "loss": float(r["loss_pct"]),
                         "delay": float(r["avg_delay_ms"]) if r["avg_delay_ms"] else None,
                         "jitter": float(r["jitter_ms"])})
        run["flows"][f["name"]] = rows
    for r in read_csv(os.path.join(d, "events.csv")):
        r["t"] = float(r["time"]) - t0
        if r["t"] <= meta["total_time"] + 1:
            run["events"].append(r)
    for r in read_csv(os.path.join(d, "link_stats.csv")):
        run["links"].setdefault(r["link"], []).append((float(r["time"]) - t0, float(r["mbps"])))
    run["traces"] = {}
    for f in meta["flows"]:
        rows = read_csv(os.path.join(d, "trace_%s.csv" % f["name"]))
        if rows:
            run["traces"][f["name"]] = [(float(r["recv_time"]) - t0, int(r["seq"]),
                                         float(r["delay_ms"]), r["status"],
                                         int(r["missing_before"])) for r in rows]
    return run


def flow_letter(run, label):
    for f in run["meta"]["flows"]:
        if label.startswith("%s:%d->" % (f["src"], f["sport"])):
            return f["name"]
    return label


# =================================================================== metrics
def flow_metrics(run, t_from=None, t_to=None):
    out = {}
    for f in run["meta"]["flows"]:
        lo = max(f["start"] + 0.5, t_from if t_from is not None else -1e9)
        hi = min(f["stop"], t_to if t_to is not None else 1e9)
        rows = [r for r in run["flows"][f["name"]] if lo <= r["t"] <= hi]
        pkts = sum(r["pkts"] for r in rows)
        lost = sum(r["lost"] for r in rows)
        delays = [(r["delay"], r["pkts"]) for r in rows if r["delay"] is not None]
        out[f["name"]] = {
            "rate": f["rate"], "pkts": pkts, "lost": lost,
            "avg_mbps": sum(r["mbps"] for r in rows) / len(rows) if rows else 0.0,
            "loss_pct": 100.0 * lost / (pkts + lost) if pkts + lost else 0.0,
            "avg_delay_ms": sum(d * n for d, n in delays) / sum(n for _, n in delays)
            if delays else 0.0,
            "max_delay_ms": max((d for d, _ in delays), default=0.0),
            "avg_jitter_ms": sum(r["jitter"] for r in rows) / len(rows) if rows else 0.0,
        }
    return out


def totals(fm):
    p = sum(x["pkts"] for x in fm.values())
    l = sum(x["lost"] for x in fm.values())
    return p, l, 100.0 * l / (p + l) if p + l else 0.0


def recovery(run, onset, horizon):
    """Seconds from onset until all active flows are healthy for 2 consecutive bins."""
    flows = run["meta"]["flows"]
    by_flow = {n: {round(r["t"], 2): r for r in rows} for n, rows in run["flows"].items()}
    bins = sorted({round(r["t"], 2) for rows in run["flows"].values() for r in rows
                   if onset < r["t"] <= horizon})

    def healthy(t):
        for f in flows:
            if not (f["start"] + 0.5 < t <= f["stop"]):
                continue
            r = by_flow[f["name"]].get(t)
            if r is None or r["loss"] >= 1.0 or r["mbps"] < 0.9 * f["rate"]:
                return False
        return True

    seen_bad = False
    for i in range(len(bins) - 1):
        if not healthy(bins[i]):
            seen_bad = True
        elif seen_bad and healthy(bins[i + 1]):
            # end of the first healthy interval: a conservative upper bound,
            # because the statistics only have 0.5 s resolution
            return bins[i] - onset
    return None if seen_bad else 0.0


def first_event(run, names, t_from, t_to):
    for e in run["events"]:
        if e["event"] in names and t_from <= e["t"] < t_to:
            return e
    return None


def congestion_events(run):
    flows = run["meta"]["flows"]
    end = max(f["stop"] for f in flows)
    starts = sorted(f["start"] for f in flows)
    out = []
    for f in flows:
        if f["start"] == starts[0]:
            continue
        nxt = min([s for s in starts if s > f["start"]] + [end])
        det = first_event(run, ("CONGESTION_DETECTED",), f["start"], nxt)
        rr = first_event(run, ("REROUTED",), f["start"], nxt)
        before = [e["event"] for e in run["events"] if e["t"] < f["start"]
                  and e["event"] in ("CONGESTION_DETECTED", "CONGESTION_CLEARED")]
        src = re.search(r"source=([\w-]+)", det["detail"]) if det else None
        out.append({
            "label": "flow %s joins (t=%ds)" % (f["name"], f["start"]),
            "detect": det["t"] - f["start"] if det else None,
            "source": src.group(1) if src else "",
            "already": bool(before) and before[-1] == "CONGESTION_DETECTED" and not det,
            "reroute": rr["t"] - f["start"] if rr else None,
            "action": "flow %s %s -> %s" % (flow_letter(run, rr["flow"]),
                                           rr["from_path"].split()[0],
                                           rr["to_path"].split()[0]) if rr else "none",
            "recovery": recovery(run, f["start"], nxt), "window": nxt - f["start"],
        })
    return out


def trace_metrics(tr):
    """Packet-level metrics of one flow from its per-packet trace."""
    gaps = [r for r in tr if r[3] == "gap"]
    late = sum(1 for r in tr if r[3] == "late")
    times = [r[0] for r in tr]
    silence = max((b - a for a, b in zip(times, times[1:])), default=0.0)
    d = sorted(r[2] for r in tr)
    pct = lambda q: d[int(q * (len(d) - 1))] if d else 0.0          # noqa: E731
    return {"received": len(tr), "gaps": len(gaps),
            "missing": sum(r[4] for r in gaps) - late,
            "largest_gap": max((r[4] for r in gaps), default=0), "silence_ms": 1000 * silence,
            "late": late, "dups": sum(1 for r in tr if r[3] == "dup"),
            "p50": pct(0.5), "p95": pct(0.95), "max": d[-1] if d else 0.0}


def final_paths(run):
    """Path of every flow at the end of the run: P1 (default) + REROUTED events."""
    paths = {f["name"]: "P1" for f in run["meta"]["flows"]}
    for e in run["events"]:
        if e["event"] == "REROUTED" and "(" in e["flow"]:
            name = flow_letter(run, e["flow"])
            if name in paths:
                paths[name] = e["to_path"].split()[0]
    return paths


def jain(xs):
    return sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs)) if xs and any(xs) else 0.0


def fmt(v, unit=" s", nd=1):
    return "-" if v is None else "%.*f%s" % (nd, v, unit)


# =================================================================== report sections
def section_congestion(runs, lines):
    if not runs:
        return
    any_run = next(iter(runs.values()))
    lines += ["## 1. Congestion (3 flows of 6 Mbps, 10 Mbps links)", ""]
    for f in any_run["meta"]["flows"]:
        lines.append("- Flow %s: %s -> %s, %.0f Mbps, t = %d-%d s"
                     % (f["name"], f["src"], f["dst"], f["rate"], f["start"], f["stop"]))
    lines += ["", "| Flow | Mode | Avg throughput (Mbps) | Loss | Avg delay (ms) | "
              "Max delay (ms) | Jitter (ms) |", "|---|---|---|---|---|---|---|"]
    fms = {m: flow_metrics(r) for m, r in runs.items()}
    for f in any_run["meta"]["flows"]:
        for m in runs:
            x = fms[m][f["name"]]
            if x["pkts"] == 0:                       # the session never started
                lines.append("| %s | %s | 0 of %.0f (could not connect) | - | - | - | - |"
                             % (f["name"], MODE_LABEL[m], x["rate"]))
                continue
            lines.append("| %s | %s | %.2f of %.0f | %.2f %% | %.1f | %.1f | %.2f |"
                         % (f["name"], MODE_LABEL[m], x["avg_mbps"], x["rate"], x["loss_pct"],
                            x["avg_delay_ms"], x["max_delay_ms"], x["avg_jitter_ms"]))
    lines += ["", "| Mode | Packets delivered | Packets lost | Overall loss |", "|---|---|---|---|"]
    for m in runs:
        p, l, pct = totals(fms[m])
        lines.append("| %s | %d | %d | %.2f %% |" % (MODE_LABEL[m], p, l, pct))
    lines += ["", "| Event | Mode | Detected after | Detected by | Rerouted after | "
              "Recovered within | Action |", "|---|---|---|---|---|---|---|"]
    ems = {m: congestion_events(r) for m, r in runs.items()}
    for i in range(len(next(iter(ems.values())))):
        for m in runs:
            e = ems[m][i]
            det = "already congested" if e["already"] else fmt(e["detect"])
            rec = fmt(e["recovery"]) if e["recovery"] is not None \
                else "not within %d s" % e["window"]
            lines.append("| %s | %s | %s | %s | %s | %s | %s |"
                         % (e["label"], MODE_LABEL[m], det, e["source"] or "-",
                            fmt(e["reroute"]), rec, e["action"]))
    lines.append("")


def section_linkfail(runs, lines):
    if not runs:
        return
    any_run = next(iter(runs.values()))
    evs = any_run["meta"]["link_events"]
    down = next(e for e in evs if e["state"] == "down")
    up = next(e for e in evs if e["state"] == "up")
    lines += ["## 2. Link failure (link s1-s2 of path P1 fails at t=%ds, repaired at t=%ds)"
              % (down["t"], up["t"]), "",
              "Two 3 Mbps flows (A: h1->h3, B: h2->h4) run on P1 when the link fails.", "",
              "| Mode | Loss during failure | Overall loss | Reaction (LINK_DOWN -> REROUTED) | "
              "Recovered within | Action |", "|---|---|---|---|---|---|"]
    for m, run in runs.items():
        t_down, t_up = down["t"], up["t"]
        during = flow_metrics(run, t_down, t_up + 0.5)
        _, _, loss_during = totals(during)
        _, _, loss_all = totals(flow_metrics(run))
        ld = first_event(run, ("LINK_DOWN",), t_down - 1, t_down + 5)
        rr = first_event(run, ("REROUTED",), t_down - 1, t_down + 5)
        react = (rr["t"] - ld["t"]) * 1000 if ld and rr else None
        rec = recovery(run, t_down, t_up)
        moved = sorted({"%s -> %s" % (flow_letter(run, e["flow"]), e["to_path"].split()[0])
                        for e in run["events"] if e["event"] == "REROUTED"
                        and "(" in e["flow"]})
        lines.append("| %s | %.2f %% | %.2f %% | %s | %s | %s |" % (
            MODE_LABEL[m], loss_during, loss_all, fmt(react, " ms", 0),
            fmt(rec) if rec is not None else "only when the link is repaired",
            ", ".join(moved) or "none (flows stay on the failed path)"))
    lines.append("")


def section_packets(link, lines):
    runs = {m: r for m, r in link.items() if r["traces"]}
    if not runs:
        return
    any_run = next(iter(runs.values()))
    down = next(e for e in any_run["meta"]["link_events"] if e["state"] == "down")
    lines += ["## 3. Packet-level view of the link failure (sequence numbers and timestamps)", "",
              "Every DATA packet carries a sequence number and the sender's timestamp. The server "
              "writes one row per packet (`trace_<flow>.csv`); a jump in the sequence numbers is "
              "a gap = lost packets. Silence = longest time without any packet of the flow. "
              "Inspect a trace with `python3 tools/trace_summary.py results/linkfail/<mode>/"
              "trace_A.csv`.", "",
              "| Mode | Flow | Packets received | Gaps | Packets lost | Largest gap (packets) | "
              "Longest silence | Out of order | Delay p50 / p95 / max (ms) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for m, run in runs.items():
        for name, tr in sorted(run["traces"].items()):
            x = trace_metrics(tr)
            sil = "%.1f s" % (x["silence_ms"] / 1000) if x["silence_ms"] >= 1000 \
                else "%.0f ms" % x["silence_ms"]
            lines.append("| %s | %s | %d | %d | %d | %d | %s | %d | %.2f / %.2f / %.1f |" % (
                MODE_LABEL[m], name, x["received"], x["gaps"], x["missing"], x["largest_gap"],
                sil, x["late"], x["p50"], x["p95"], x["max"]))
    lines += ["", "With static routing the sequence numbers stop for the whole failure (t=%d s) "
              "and jump when the link is repaired; with the SDN controller only a few packets "
              "around t=%d s are missing." % (down["t"], down["t"]), ""]


def section_flows(flowruns, lines):
    if not flowruns:
        return []
    any_run = next(iter(flowruns.values()))
    rate = any_run["meta"]["rate"]
    cap = any_run["meta"]["link_capacity_mbps"]
    lines += ["## 5. Increasing number of flows (%g Mbps each; P1 = %g Mbps, 3 paths = %g Mbps)"
              % (rate, cap, 3 * cap), "",
              "N flows join one by one (every 2 s) on the default path P1 and all run until 20 s "
              "after the last one joined. Throughput and fairness are measured while all flows "
              "run (from 6 s after the last flow joined); loss and delay over the whole run. "
              "Fairness = Jain's index of throughput / requested rate (1 = every flow gets what "
              "it asked for).", "",
              "| Flows | Offered (Mbps) | Mode | Delivered (Mbps) | Loss | Avg delay (ms) | "
              "Max delay (ms) | Fairness | Reroutes | Flows on P1 / P2 / P3 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    rows = []
    for (m, n), run in sorted(flowruns.items(), key=lambda k: (k[0][1], MODES.index(k[0][0]))):
        fl = run["meta"]["flows"]
        t_all = max(f["start"] for f in fl) + 6
        t_end = min(f["stop"] for f in fl) - 0.5
        steady = flow_metrics(run, t_all, t_end)
        whole = flow_metrics(run)
        delivered = sum(x["avg_mbps"] for x in steady.values())
        _, _, loss = totals(whole)
        pk = sum(x["pkts"] for x in whole.values()) or 1
        delay = sum(x["avg_delay_ms"] * x["pkts"] for x in whole.values()) / pk
        maxd = max(x["max_delay_ms"] for x in whole.values())
        fair = jain([steady[f["name"]]["avg_mbps"] / f["rate"] for f in fl])
        nre = sum(1 for e in run["events"] if e["event"] == "REROUTED")
        paths = final_paths(run)
        per = [sum(1 for p in paths.values() if p == q) for q in ("P1", "P2", "P3")]
        offered = sum(f["rate"] for f in fl)
        rows.append({"mode": m, "n": n, "offered": offered, "delivered": delivered,
                     "loss": loss, "delay": delay, "maxd": maxd, "fair": fair, "per": per})
        lines.append("| %d | %.0f | %s | %.2f | %.2f %% | %.1f | %.0f | %.3f | %d | %d / %d / %d |"
                     % (n, offered, MODE_LABEL[m], delivered, loss, delay, maxd, fair, nre, *per))
    lines.append("")
    return rows


def section_sweep(sweep, lines):
    if not sweep:
        return []
    lines += ["## 4. Load sweep (2 flows on P1; offered load = both flows together)", "",
              "| Per-flow rate | Offered load on P1 | Mode | Loss | Avg delay (ms) | "
              "Avg throughput per flow (Mbps) | Reroutes |", "|---|---|---|---|---|---|---|"]
    rows = []
    for (m, rate), run in sorted(sweep.items(), key=lambda k: (k[0][1], MODES.index(k[0][0]))):
        size = run["meta"]["packet_size"]
        offered = 2 * rate * (size + 42) / size / run["meta"]["link_capacity_mbps"] * 100
        b_start = max(f["start"] for f in run["meta"]["flows"])
        fm = flow_metrics(run, b_start + 1)          # while both flows are running
        _, _, loss = totals(fm)
        delay = sum(x["avg_delay_ms"] for x in fm.values()) / len(fm)
        thr = sum(x["avg_mbps"] for x in fm.values()) / len(fm)
        nre = sum(1 for e in run["events"] if e["event"] == "REROUTED")
        rows.append((m, rate, offered, loss, delay, thr))
        lines.append("| %.0f Mbps | %.0f %% | %s | %.2f %% | %.1f | %.2f | %d |"
                     % (rate, offered, MODE_LABEL[m], loss, delay, thr, nre))
    lines.append("")
    return rows


# =================================================================== plots
def style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def top_legend(fig, ax, ncol):
    h, l = ax.get_legend_handles_labels()
    fig.legend(h, l, loc="upper right", ncol=ncol, frameon=False, fontsize=9,
               bbox_to_anchor=(0.99, 0.995))


def plot_congestion(cong, plt):
    modes = list(cong)
    total = max(r["meta"]["total_time"] for r in cong.values())
    fig, axes = plt.subplots(len(modes), 1, figsize=(10, 2.9 * len(modes)), sharex=True,
                             squeeze=False)
    for ax, m in zip(axes[:, 0], modes):
        run = cong[m]
        for f in run["meta"]["flows"]:
            rows = [r for r in run["flows"][f["name"]] if 0 <= r["t"] <= total]
            ax.plot([r["t"] for r in rows], [r["mbps"] for r in rows], linewidth=2,
                    color=FLOW_COLORS.get(f["name"], INK),
                    label="Flow %s (%s->%s)" % (f["name"], f["src"], f["dst"]))
        for f in run["meta"]["flows"][1:]:
            ax.axvline(f["start"], color=GRID, linewidth=1.2)
        for e in run["events"]:
            if e["event"] == "REROUTED" and "(" in e["flow"]:
                ax.axvline(e["t"], color=INK2, linewidth=0.8, linestyle=(0, (2, 2)))
                ax.text(e["t"] + 0.4, 11.2, "reroute %s -> %s" % (
                    flow_letter(run, e["flow"]), e["to_path"].split()[0]),
                    fontsize=8, color=INK2, va="top")
        ax.set_ylim(0, 11.5)
        ax.set_xlim(0, total)
        ax.set_ylabel("Received Mbps")
        ax.set_title(MODE_LABEL[m], loc="left")
        style(ax)
    axes[-1, 0].set_xlabel("Time (s)  -  thin gray lines: a new flow joins and overloads P1")
    fig.suptitle("Congestion: per-flow UDP throughput (each flow sends 6 Mbps)",
                 x=0.01, ha="left", fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    top_legend(fig, axes[0, 0], 3)
    fig.savefig(os.path.join(RESULTS, "congestion_throughput.png"), dpi=150)
    plt.close(fig)

    fig, axes = plt.subplots(len(modes), 1, figsize=(10, 2.9 * len(modes)), sharex=True,
                             squeeze=False)
    cap = float(next(iter(cong.values()))["meta"]["link_capacity_mbps"])
    for ax, m in zip(axes[:, 0], modes):
        for name, lk in (("P1", "s1->s2"), ("P2", "s1->s3"), ("P3", "s1->s4")):
            pts = [(t, 100 * v / cap) for t, v in cong[m]["links"].get(lk, []) if 0 <= t <= total]
            ax.plot([p[0] for p in pts], [p[1] for p in pts], linewidth=2,
                    color=PATH_COLORS[name], label="%s (link %s)" % (name, lk))
        ax.axhline(80, color=INK2, linewidth=1)
        ax.text(total - 0.3, 82, "threshold 80%", ha="right", fontsize=8, color=INK2)
        ax.set_ylim(0, 115)
        ax.set_xlim(0, total)
        ax.set_ylabel("Utilisation %")
        ax.set_title(MODE_LABEL[m], loc="left")
        style(ax)
    axes[-1, 0].set_xlabel("Time (s)")
    fig.suptitle("Congestion: load on each path (controller port statistics)",
                 x=0.01, ha="left", fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    top_legend(fig, axes[0, 0], 3)
    fig.savefig(os.path.join(RESULTS, "congestion_paths.png"), dpi=150)
    plt.close(fig)

    fms = {m: flow_metrics(r) for m, r in cong.items()}
    names = [f["name"] for f in next(iter(cong.values()))["meta"]["flows"]]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    width = 0.24
    for ax, key, title, unit in ((axes[0], "loss_pct", "Packet loss per flow (%)", "%"),
                                 (axes[1], "avg_delay_ms", "Average one-way delay (ms)", "")):
        top = max(fms[m][n][key] for m in modes for n in names) or 1
        for i, m in enumerate(modes):
            xs = [j + (i - (len(modes) - 1) / 2) * (width + 0.03) for j in range(len(names))]
            vals = [fms[m][n][key] for n in names]
            ax.bar(xs, vals, width=width, color=MODE_COLORS[m], label=MODE_LABEL[m])
            for x, v in zip(xs, vals):
                ax.text(x, v + top * 0.02, ("%.1f" % v) + unit, ha="center", fontsize=7.5,
                        color=INK2)
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(["Flow %s" % n for n in names])
        ax.set_ylim(0, top * 1.2)
        ax.set_title(title, loc="left")
        style(ax)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=3, frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    fig.savefig(os.path.join(RESULTS, "congestion_summary.png"), dpi=150)
    plt.close(fig)


def plot_linkfail(link, plt):
    modes = list(link)
    any_run = next(iter(link.values()))
    total = any_run["meta"]["total_time"]
    ev = any_run["meta"]["link_events"]
    t_down = next(e["t"] for e in ev if e["state"] == "down")
    t_up = next(e["t"] for e in ev if e["state"] == "up")
    fig, axes = plt.subplots(len(modes), 1, figsize=(10, 2.9 * len(modes)), sharex=True,
                             squeeze=False)
    for ax, m in zip(axes[:, 0], modes):
        run = link[m]
        ax.axvspan(t_down, t_up, color="#f3e1e1", linewidth=0)
        ax.text(t_down + 0.3, 5.7, "link s1-s2 (path P1) down", fontsize=8, color=INK2, va="top")
        for i, f in enumerate(run["meta"]["flows"]):
            rows = [r for r in run["flows"][f["name"]] if 0 <= r["t"] <= total]
            # both flows send the same rate, so draw the second dashed to keep both visible
            ax.plot([r["t"] for r in rows], [r["mbps"] for r in rows], linewidth=2,
                    color=FLOW_COLORS.get(f["name"], INK), linestyle="-" if i == 0 else (0, (4, 3)),
                    label="Flow %s (%s->%s)" % (f["name"], f["src"], f["dst"]))
        ax.set_ylim(0, 6)
        ax.set_xlim(0, total)
        ax.set_ylabel("Received Mbps")
        ax.set_title(MODE_LABEL[m], loc="left")
        style(ax)
    axes[-1, 0].set_xlabel("Time (s)")
    fig.suptitle("Link failure: per-flow UDP throughput (each flow sends 3 Mbps)",
                 x=0.01, ha="left", fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    top_legend(fig, axes[0, 0], 2)
    fig.savefig(os.path.join(RESULTS, "linkfail_throughput.png"), dpi=150)
    plt.close(fig)


def plot_packets(link, plt):
    runs = {m: r for m, r in link.items() if r["traces"].get("A")}
    if not runs:
        return
    any_run = next(iter(runs.values()))
    total = any_run["meta"]["total_time"]
    ev = any_run["meta"]["link_events"]
    t_down = next(e["t"] for e in ev if e["state"] == "down")
    t_up = next(e["t"] for e in ev if e["state"] == "up")
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.2))
    panels = [(axes[0, 0], "static"), (axes[0, 1], "app_aware")]
    for ax, m in panels:
        if m not in runs:
            ax.set_visible(False)
            continue
        tr = runs[m]["traces"]["A"]
        ax.axvspan(t_down, t_up, color="#f3e1e1", linewidth=0)
        ax.plot([r[0] for r in tr], [r[1] for r in tr], ".", markersize=1.5,
                color=MODE_COLORS[m])
        x = trace_metrics(tr)
        big = max((r for r in tr if r[3] == "gap"), key=lambda r: r[4], default=None)
        if big is not None:
            ax.annotate("seq %d-%d never arrived\n(%d packets, %.1f s without packets)"
                        % (big[1] - big[4], big[1] - 1, big[4], x["silence_ms"] / 1000)
                        if x["silence_ms"] >= 1000 else
                        "only %d packets lost\n(%.0f ms without packets)"
                        % (x["missing"], x["silence_ms"]),
                        xy=(big[0], big[1]), xytext=(0.03, 0.97), textcoords="axes fraction",
                        va="top", fontsize=8.5, color=INK,
                        arrowprops=dict(arrowstyle="-", color=INK2, linewidth=0.8))
        ax.set_xlim(0, total)
        ax.set_xlabel("Receive time (s)  -  red band: link s1-s2 down")
        ax.set_ylabel("Sequence number")
        ax.set_title("%s: sequence numbers of flow A" % MODE_LABEL[m], loc="left")
        style(ax)
    # zoom on the moment of the failure for the SDN mode
    ax = axes[1, 0]
    m = "app_aware" if "app_aware" in runs else next(iter(runs))
    gap = max((r for r in runs[m]["traces"]["A"] if r[3] == "gap" and r[0] > t_down - 1),
              key=lambda r: r[4], default=None)
    lo = (gap[0] - 0.25) if gap else t_down - 0.4
    tr = [r for r in runs[m]["traces"]["A"] if lo <= r[0] <= lo + 0.5]
    ax.plot([r[0] for r in tr], [r[1] for r in tr], "o", markersize=2.6, color=MODE_COLORS[m])
    if gap:
        ax.annotate("gap: seq %d-%d lost\nthen the flow continues on the new path"
                    % (gap[1] - gap[4], gap[1] - 1), xy=(gap[0], gap[1]),
                    xytext=(0.03, 0.97), textcoords="axes fraction", va="top", fontsize=8.5,
                    arrowprops=dict(arrowstyle="-", color=INK2, linewidth=0.8))
    ax.set_xlabel("Receive time (s)")
    ax.set_ylabel("Sequence number")
    ax.set_title("%s: zoom on the failure (0.5 s)" % MODE_LABEL[m], loc="left")
    style(ax)
    ax = axes[1, 1]
    top = 1.0
    for m, run in runs.items():
        tr = run["traces"]["A"]
        ax.plot([r[0] for r in tr], [r[2] for r in tr], ".", markersize=1.2,
                color=MODE_COLORS[m], label=MODE_LABEL[m])
        d = sorted(r[2] for r in tr)
        top = max(top, d[int(0.999 * (len(d) - 1))])
    ax.axvspan(t_down, t_up, color="#f3e1e1", linewidth=0)
    ax.set_ylim(0, top * 1.4)
    ax.set_xlim(0, total)
    ax.set_xlabel("Receive time (s)")
    ax.set_ylabel("ms")
    ax.set_title("One-way delay of every packet = receive time - timestamp", loc="left")
    ax.legend(frameon=False, fontsize=8.5, markerscale=6, loc="upper right")
    style(ax)
    fig.suptitle("Link failure, packet by packet: sequence numbers and timestamps (flow A, 3 Mbps)",
                 x=0.01, ha="left", fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(os.path.join(RESULTS, "linkfail_packets.png"), dpi=150)
    plt.close(fig)


def plot_flows(rows, plt):
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.2))
    ns = sorted({r["n"] for r in rows})
    cap = 10.0
    ax = axes[0, 0]
    offered = sorted({(r["n"], r["offered"]) for r in rows})
    ax.plot([o[0] for o in offered], [o[1] for o in offered], color=INK2, linewidth=1.2,
            linestyle=(0, (4, 3)), label="Offered (all flows)")
    for c, text in ((cap, "capacity of one path (%g Mbps)" % cap),
                    (3 * cap, "capacity of all 3 paths (%g Mbps)" % (3 * cap))):
        ax.axhline(c, color=GRID, linewidth=1.2)
        ax.text(ns[-1], c + 0.5, text, fontsize=8, color=INK2, ha="right")
    for key, title, axx, log in (("delivered", "Delivered throughput, all flows (Mbps)", axes[0, 0], False),
                                 ("loss", "Packet loss (%)", axes[0, 1], False),
                                 ("delay", "Average one-way delay (ms, log scale)", axes[1, 0], True)):
        for m in MODES:
            pts = sorted((r["n"], r[key]) for r in rows if r["mode"] == m)
            if not pts:
                continue
            axx.plot([p[0] for p in pts], [max(p[1], 0.05) if log else p[1] for p in pts],
                     linewidth=2, marker="o", markersize=6, color=MODE_COLORS[m],
                     label=MODE_LABEL[m])
        if log:
            axx.set_yscale("log")
            axx.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: "%g" % v))
            axx.yaxis.set_minor_formatter(plt.NullFormatter())
        else:
            axx.set_ylim(bottom=0, top=max(1.0, axx.get_ylim()[1]))
        axx.set_xticks(ns)
        axx.set_xlabel("Number of flows (%g Mbps each)" % (rows[0]["offered"] / rows[0]["n"]))
        axx.set_title(title, loc="left")
        style(axx)
    axes[0, 0].legend(frameon=False, fontsize=8.5, loc="upper left")
    ax = axes[1, 1]
    sdn = sorted((r for r in rows if r["mode"] == "app_aware"), key=lambda r: r["n"])
    bottom = [0] * len(sdn)
    xs = list(range(len(sdn)))
    for i, (pname, col) in enumerate(PATH_COLORS.items()):
        vals = [r["per"][i] for r in sdn]
        ax.bar(xs, vals, bottom=bottom, width=0.6, color=col, label=pname)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_xticks(xs)
    ax.set_xticklabels([str(r["n"]) for r in sdn])
    ax.set_xlabel("Number of flows")
    ax.set_title("App-aware: flows on each path at the end (static: all on P1)", loc="left")
    ax.legend(frameon=False, fontsize=8.5, loc="upper left")
    style(ax)
    fig.suptitle("Increasing the number of flows: static routing vs. SDN rerouting",
                 x=0.01, ha="left", fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(os.path.join(RESULTS, "flows.png"), dpi=150)
    plt.close(fig)


def plot_sweep(rows, plt):
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    for ax, idx, title, log in ((axes[0], 3, "Packet loss (%)", False),
                                (axes[1], 4, "Average one-way delay (ms, log scale)", True)):
        for m in MODES:
            pts = sorted((r[2], r[idx]) for r in rows if r[0] == m)
            if not pts:
                continue
            ax.plot([p[0] for p in pts], [max(p[1], 0.05) if log else p[1] for p in pts],
                    linewidth=2, marker="o", markersize=6, color=MODE_COLORS[m],
                    label=MODE_LABEL[m])
        if log:
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: "%g" % v))
            ax.yaxis.set_minor_formatter(plt.NullFormatter())
        ax.axvline(80, color=INK2, linewidth=1, linestyle=(0, (2, 2)))
        ax.axvline(100, color=GRID, linewidth=1.2)
        ax.annotate("threshold 80%", xy=(80, 1), xycoords=("data", "axes fraction"),
                    xytext=(-3, -4), textcoords="offset points", ha="right", va="top",
                    fontsize=8, color=INK2)
        ax.set_xlabel("Offered load on P1 (% of link capacity)")
        ax.set_title(title, loc="left")
        style(ax)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=3, frameon=False, fontsize=9)
    fig.suptitle("Load sweep: static vs. SDN rerouting at different loads", x=0.01, ha="left",
                 fontweight="bold")
    fig.tight_layout(rect=(0, 0.08, 1, 0.94))
    fig.savefig(os.path.join(RESULTS, "sweep.png"), dpi=150)
    plt.close(fig)


def main():
    cong = {}
    link = {}
    for m in MODES:
        r = load_run(os.path.join(RESULTS, "congestion", m))
        if r:
            cong[m] = r
        r = load_run(os.path.join(RESULTS, "linkfail", m))
        if r:
            link[m] = r
    sweep = {}
    for d in sorted(glob.glob(os.path.join(RESULTS, "sweep", "*_r*"))):
        mm = re.match(r"(static|adaptive|app_aware)_r([\d.]+)$", os.path.basename(d))
        r = load_run(d) if mm else None
        if r:
            sweep[(mm.group(1), float(mm.group(2)))] = r
    flowruns = {}
    for d in sorted(glob.glob(os.path.join(RESULTS, "flows", "*_n*"))):
        mm = re.match(r"(static|adaptive|app_aware)_n(\d+)$", os.path.basename(d))
        r = load_run(d) if mm else None
        if r:
            flowruns[(mm.group(1), int(mm.group(2)))] = r
    if not (cong or link or sweep or flowruns):
        sys.exit("No results found - run the experiments first (see README).")

    lines = ["# Performance evaluation - static vs. SDN rerouting", "",
             "Generated by analyze.py from the logs in results/. Modes: **Static** = baseline "
             "(no rerouting), **Adaptive** = rerouting from OpenFlow switch statistics, "
             "**App-aware** = adaptive + QoS feedback from the UDP application (proposed).", ""]
    section_congestion(cong, lines)
    section_linkfail(link, lines)
    section_packets(link, lines)
    rows = section_sweep(sweep, lines)
    frows = section_flows(flowruns, lines)
    path = os.path.join(RESULTS, "comparison.md")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))
    print("wrote", path)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed - skipping graphs (sudo apt install python3-matplotlib)")
        return
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold",
                         "axes.labelcolor": INK2, "text.color": INK})
    if cong:
        plot_congestion(cong, plt)
    if link:
        plot_linkfail(link, plt)
        plot_packets(link, plt)
    if rows:
        plot_sweep(rows, plt)
    if frows:
        plot_flows(frows, plt)
    print("wrote graphs in", RESULTS)


if __name__ == "__main__":
    main()
