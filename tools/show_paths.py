#!/usr/bin/env python3
"""
show_paths.py - Which path is every UDP flow using right now?

Reads the per-flow rules (priority 300) that the controller installed on the
ingress switch s1 and translates the output port into the path name.

    mininet> sh python3 tools/show_paths.py
    $ sudo python3 tools/show_paths.py          (from a normal terminal)
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import netconfig as nc  # noqa: E402


def field(line, name):
    for part in line.replace(" ", ",").split(","):
        if part.startswith(name + "="):
            return part.split("=", 1)[1]
    return None


def out_port(line):
    act = line.split("actions=")[-1]
    val = act.split("output:")[-1].split(",")[0].strip('"')
    if "eth" in val:
        val = val.split("eth")[-1]
    return int(val) if val.isdigit() else None


def main():
    try:
        out = subprocess.run(["ovs-ofctl", "dump-flows", "s1"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.exit("could not run ovs-ofctl (%s) - is the Mininet network running?" % e)
    pm = nc.port_map()
    paths = {pm[("s1", nb)]: nc.path_name(p) for p in nc.all_paths("s1", "s5")
             for nb in [p[1]]}
    host_of = {v["ip"]: h for h, v in nc.HOSTS.items()}
    rows = []
    for line in out.splitlines():
        if "priority=300" not in line:
            continue
        port = out_port(line)
        if port not in paths:
            continue
        src, dst = field(line, "nw_src"), field(line, "nw_dst")
        nbytes = int(field(line, "n_bytes") or 0)
        dur = float((field(line, "duration") or "0s").rstrip("s") or 0)
        rows.append(("%s:%s -> %s:%s" % (host_of.get(src, src), field(line, "tp_src"),
                                         host_of.get(dst, dst), field(line, "tp_dst")),
                     paths[port], nbytes, dur))
    if not rows:
        print("No per-flow UDP rules on s1 yet (start some clients first).")
        return
    print("UDP flows on s1 (per-flow rules installed by the controller):")
    print("  %-26s %-14s %s" % ("flow", "path", "traffic since the rule was installed"))
    for name, path, nbytes, dur in sorted(rows, key=lambda r: (r[1], r[0])):
        rate = nbytes * 8 / dur / 1e6 if dur > 0 else 0.0
        print("  %-26s %-14s %6.2f MB in %4.0f s (about %.1f Mbps)"
              % (name, path, nbytes / 1e6, dur, rate))
    counts = {}
    for _, path, _, _ in rows:
        counts[path.split()[0]] = counts.get(path.split()[0], 0) + 1
    print("Flows per path:  " + "   ".join("%s: %d" % (p, counts.get(p, 0))
                                          for p in ("P1", "P2", "P3")))


if __name__ == "__main__":
    main()
