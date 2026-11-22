#!/usr/bin/env python3
"""
trace_summary.py - Packet-level view of one flow from the server's per-packet
trace (udp_server.py --trace, written for every link-failure experiment).

    python3 tools/trace_summary.py results/linkfail/static/trace_A.csv
    python3 tools/trace_summary.py results/linkfail/app_aware/trace_A.csv --head 5
    python3 tools/trace_summary.py --brief /tmp/d2/trace_*.csv      (one line per flow)

Shows the first packets (sequence number, sender timestamp, receive time,
one-way delay), every gap in the sequence numbers (= lost packets) with the
time it happened, out-of-order packets and delay statistics. If the trace is
in an experiment folder, times are also shown relative to the experiment start
(t = 0), so a gap can be matched with the link failure at t = 12 s.
"""

import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))
from protocol import fmt_time  # noqa: E402


def load(path):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append((float(r["recv_time"]), int(r["seq"]), float(r["send_time"]),
                         float(r["delay_ms"]), r["status"], int(r["missing_before"])))
    return rows


def experiment_t0(path):
    meta = os.path.join(os.path.dirname(os.path.abspath(path)), "scenario.json")
    if os.path.exists(meta):
        with open(meta) as fh:
            m = json.load(fh)
        return m.get("t0"), m
    return None, None


def final_gaps(rows):
    """Gaps in the sequence numbers with the packets of each gap that never arrived.
    Returns [(first_seq, n, lost, row_after, row_before)] - lost < n means some
    packets of the gap arrived later (out of order, e.g. after a reroute)."""
    missing, gaps, prev = set(), [], None
    for r in rows:
        if r[4] == "gap":
            first = r[1] - r[5]
            missing.update(range(first, r[1]))
            gaps.append((first, r[5], r, prev))
        elif r[4] == "late":
            missing.discard(r[1])
        if r[4] in ("ok", "gap"):
            prev = r
    return [(first, n, sum(1 for q in range(first, first + n) if q in missing), after, before)
            for first, n, after, before in gaps]


def brief(path):
    """One line per flow: packets, lost packets (and when), out-of-order packets, delay."""
    rows = load(path)
    flow = os.path.basename(path)[len("trace_"):-len(".csv")]
    if not rows:
        print("%-4s no packets" % flow)
        return
    gaps = [g for g in final_gaps(rows) if g[2] > 0]
    lost = sum(g[2] for g in gaps)
    late = sum(1 for r in rows if r[4] == "late")
    d = [r[3] for r in rows]
    text = "%-4s %6d packets, seq %d..%d, lost %d" % (flow, len(rows), min(r[1] for r in rows),
                                                     max(r[1] for r in rows), lost)
    if gaps:
        first, n, nl, after, before = max(gaps, key=lambda g: g[2])
        span = "seq %d" % first if n == 1 else "seq %d-%d" % (first, first + n - 1)
        text += " (%s%s at %s, %.0f ms without packets)" % (
            "%d gaps, biggest " % len(gaps) if len(gaps) > 1 else "", span, fmt_time(after[0]),
            (after[0] - before[0]) * 1000 if before else 0.0)
    text += ", out of order %d" % late
    text += ", delay avg %.1f / max %.1f ms" % (sum(d) / len(d), max(d))
    print(text)


def main():
    ap = argparse.ArgumentParser(description="Summarise per-packet traces")
    ap.add_argument("trace", nargs="+", help="trace_<flow>.csv written by udp_server.py --trace")
    ap.add_argument("--head", type=int, default=8, help="how many first packets to list")
    ap.add_argument("--gaps", type=int, default=15, help="list at most this many gaps")
    ap.add_argument("--brief", action="store_true", help="one line per flow")
    args = ap.parse_args()
    for i, path in enumerate(args.trace):
        if args.brief:
            brief(path)
        else:
            if i:
                print("\n" + "=" * 100 + "\n")
            detail(path, args)


def detail(path, args):
    rows = load(path)
    if not rows:
        print("%s: empty trace" % path)
        return
    t0, meta = experiment_t0(path)

    def when(t):
        return fmt_time(t) + ("  (t=%6.2f s)" % (t - t0) if t0 else "")

    flow = os.path.basename(path)[len("trace_"):-len(".csv")]
    seqs = [r[1] for r in rows]
    print("Flow %s: %d packets received, sequence numbers %d..%d   [%s]"
          % (flow, len(rows), min(seqs), max(seqs), path))
    if meta:
        evs = ", ".join("link %s-%s %s at t=%.0f s" % (e["a"], e["b"], e["state"], e["t"])
                        for e in meta.get("link_events", []))
        print("Experiment: %s, mode %s%s" % (meta["scenario"], meta["mode"],
                                             "; " + evs if evs else ""))

    print("\nFirst packets:")
    print("  %-7s  %-15s  %-15s  %s" % ("seq", "sent (header)", "received", "one-way delay"))
    for r in rows[:args.head]:
        print("  %-7d  %-15s  %-15s  %.2f ms" % (r[1], fmt_time(r[2]), fmt_time(r[0]), r[3]))

    gaps = final_gaps(rows)
    lost = sum(g[2] for g in gaps)
    late = sum(1 for r in rows if r[4] == "late")
    dups = sum(1 for r in rows if r[4] == "dup")
    print("\nGaps in the sequence numbers: %d gap%s, %d packets lost"
          % (len(gaps), "" if len(gaps) == 1 else "s", lost))
    biggest = sorted(gaps, key=lambda g: -g[1])[:args.gaps]
    for first, n, nl, after, before in sorted(biggest, key=lambda g: g[0]):     # in time order
        span = "seq %d" % first if n == 1 else "seq %d-%d" % (first, first + n - 1)
        silent = (after[0] - before[0]) * 1000 if before else 0.0
        fate = "lost" if nl == n else ("arrived later (out of order)" if nl == 0
                                       else "%d lost, %d arrived later" % (nl, n - nl))
        print("  %-17s %5d packets %-28s last packet before: %s   next: %s   (%.0f ms "
              "with nothing received)" % (span, n, fate, when(before[0]) if before else "-",
                                          when(after[0]), silent))
    if len(gaps) > args.gaps:
        print("  ... %d smaller gaps not listed" % (len(gaps) - args.gaps))
    print("Out of order (late) packets: %d, duplicates: %d" % (late, dups))

    d = sorted(r[3] for r in rows)
    print("One-way delay: min %.2f ms, avg %.2f ms, p95 %.2f ms, max %.2f ms"
          % (d[0], sum(d) / len(d), d[int(0.95 * (len(d) - 1))], d[-1]))


if __name__ == "__main__":
    main()
