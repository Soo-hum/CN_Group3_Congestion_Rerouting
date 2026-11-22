#!/usr/bin/env python3
"""
experiment.py - Run ONE automated experiment: Mininet + POX controller + UDP
client/server application, then save all logs.

    sudo python3 experiment.py --scenario congestion --mode app_aware
    sudo python3 experiment.py --scenario linkfail   --mode static
    sudo python3 experiment.py --scenario sweep --rate 5 --mode adaptive
    sudo python3 experiment.py --scenario flows --flows 8 --mode app_aware

The script starts the POX controller itself (with the right mode and log
folder) and stops it at the end. Use --no-controller if you started one
yourself with run_controller.sh.

Scenarios (all core links 10 Mbps; every new flow starts on path P1)

  congestion  A: h1->h3  6 Mbps  2-52 s
              B: h2->h4  6 Mbps 12-52 s   -> P1 overloaded (12.4 Mbps on 10)
              C: h1->h4  6 Mbps 27-47 s   -> P1 overloaded again
  linkfail    A: h1->h3  3 Mbps, B: h2->h4 3 Mbps (2-40 s), both on P1;
              link s1-s2 (P1) fails at t=12 s and is repaired at t=24 s
  sweep       A: h1->h3, B: h2->h4 at --rate Mbps each (A 2-32 s, B 7-32 s);
              repeated for several rates = several load levels on P1
  flows       --flows N flows of --rate Mbps each (default 2), named A, B, C, ...
              using all four host pairs (h1->h3, h2->h4, h1->h4, h2->h3); a new
              flow joins every 2 s and all run until 20 s after the last one
              joined. Repeated for N = 1..12 = increasing number of flows.

The link failure scenario also writes a per-packet trace (trace_<flow>.csv:
sequence number, send/receive timestamps, delay, gap/late status).

Servers: one UDP server on h3 (port 5001) and one on h4 (port 5002). The h4
server handles flows B and C at the same time (concurrent sessions).

Output: results/<scenario>/<mode>[_r<rate> | _n<flows>]/
    flow_<X>.csv        per-interval QoS measured by the server
    trace_<X>.csv       per-packet trace (link failure scenario)
    events.csv, link_stats.csv, flow_stats.csv, app_qos.csv   (controller)
    controller.log, hosts.log, flowtable_s1.txt, scenario.json
"""

import argparse
import json
import os
import pwd
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import netconfig as nc  # noqa: E402
from topology import build_network, prepare_hosts  # noqa: E402
from mininet.log import setLogLevel, info, error  # noqa: E402

SERVERS = {"h3": 5001, "h4": 5002}
PACKET_SIZE = 1200


def flow(name, src, dst, sport, rate, start, stop):
    return {"name": name, "src": src, "dst": dst, "sport": sport, "dport": SERVERS[dst],
            "rate": rate, "start": start, "stop": stop}


FLOW_PAIRS = [("h1", "h3"), ("h2", "h4"), ("h1", "h4"), ("h2", "h3")]
MAX_FLOWS = 16                      # 2 servers x 8 sessions


def scenario(name, rate, n_flows=1):
    if name == "congestion":
        return {"total": 56, "snapshots": [8, 22, 40], "link_events": [],
                "flows": [flow("A", "h1", "h3", 40001, 6.0, 2, 52),
                          flow("B", "h2", "h4", 40002, 6.0, 12, 52),
                          flow("C", "h1", "h4", 40003, 6.0, 27, 47)]}
    if name == "linkfail":
        return {"total": 42, "snapshots": [8, 16, 30],
                "link_events": [{"t": 12, "a": "s1", "b": "s2", "state": "down"},
                                {"t": 24, "a": "s1", "b": "s2", "state": "up"}], "trace": True,
                "flows": [flow("A", "h1", "h3", 40001, 3.0, 2, 40),
                          flow("B", "h2", "h4", 40002, 3.0, 2, 40)]}
    if name == "sweep":
        return {"total": 34, "snapshots": [20], "link_events": [], "rate": rate,
                "flows": [flow("A", "h1", "h3", 40001, rate, 2, 32),
                          flow("B", "h2", "h4", 40002, rate, 7, 32)]}
    if name == "flows":
        join_gap = 2.0
        stop = 2 + join_gap * (n_flows - 1) + 20
        flows = []
        for i in range(n_flows):
            src, dst = FLOW_PAIRS[i % len(FLOW_PAIRS)]
            flows.append(flow(chr(ord("A") + i), src, dst, 40001 + i, rate, 2 + join_gap * i, stop))
        return {"total": stop + 2, "snapshots": [stop - 2], "link_events": [], "rate": rate,
                "n_flows": n_flows, "flows": flows}
    raise ValueError(name)


# ---------------------------------------------------------------- controller
def port_in_use(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def find_pox():
    if os.environ.get("POX_DIR"):
        return os.environ["POX_DIR"]
    user = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    return os.path.join(pwd.getpwnam(user).pw_dir, "pox")


def start_controller(mode, log_dir, extra):
    pox = find_pox()
    if not os.path.exists(os.path.join(pox, "pox.py")):
        error("!!! POX not found in %s. Install: git clone https://github.com/noxrepo/pox.git "
              "~/pox  (or set POX_DIR)\n" % pox)
        return None
    if port_in_use(nc.CONTROLLER_PORT):
        error("!!! Port %d is busy - another controller is running. Stop it "
              "(sudo pkill -f pox.py, or sudo systemctl stop openvswitch-testcontroller) "
              "or use --no-controller.\n" % nc.CONTROLLER_PORT)
        return None
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(HERE, "controller") + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "pox.py", "log.level", "--INFO", "openflow.of_01",
           "--port=%d" % nc.CONTROLLER_PORT, "congestion_controller", "--mode=" + mode,
           "--log_dir=" + log_dir] + extra
    logf = open(os.path.join(log_dir, "controller.log"), "w")
    proc = subprocess.Popen(cmd, cwd=pox, env=env, stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(50):                               # wait until it accepts connections
        time.sleep(0.2)
        if proc.poll() is not None:
            break
        try:
            socket.create_connection(("127.0.0.1", nc.CONTROLLER_PORT), timeout=0.5).close()
            info("*** POX controller running (mode=%s, pid %d)\n" % (mode, proc.pid))
            return proc
        except OSError:
            pass
    error("!!! Controller did not start - see %s/controller.log\n" % log_dir)
    proc.kill()
    return None


def stop_controller(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------- experiment
def run(args, outdir):
    sc = scenario(args.scenario, args.rate, args.flows)
    os.makedirs(outdir, exist_ok=True)
    for f in os.listdir(outdir):                       # fresh results for this run
        if f.endswith((".csv", ".log", ".txt", ".json")):
            os.remove(os.path.join(outdir, f))
    ctrl = None
    if not args.no_controller:
        ctrl = start_controller(args.mode, outdir, ["--" + kv for kv in args.ctrl])
        if ctrl is None:
            return 1
    net = build_network(userspace=args.userspace)
    procs = []
    logs = []
    try:
        net.start()
        prepare_hosts(net, args.userspace)
        info("*** Waiting for switches to connect to the controller...\n")
        if not net.waitConnected(timeout=30):
            error("!!! Switches did not connect - is the controller running?\n")
            return 1
        loss = net.pingAll(timeout="1")
        if loss > 0:
            loss = net.pingAll(timeout="1")

        t0 = time.time() + 4.0
        py = sys.executable
        open(os.path.join(outdir, "hosts.log"), "w").close()
        hosts_log = open(os.path.join(outdir, "hosts.log"), "a")
        logs.append(hosts_log)
        app = os.path.join(HERE, "app")
        for host, port in SERVERS.items():
            cmd = [py, os.path.join(app, "udp_server.py"), "--port", str(port),
                   "--duration", "%.1f" % (t0 + sc["total"] + 3 - time.time()),
                   "--out-dir", outdir, "--interval", "0.5"] + (["--trace"] if sc.get("trace") else [])
            procs.append(net.get(host).popen(cmd, stdout=hosts_log, stderr=subprocess.STDOUT,
                                             cwd=HERE))
        time.sleep(1.0)
        for f in sc["flows"]:
            cmd = [py, os.path.join(app, "udp_client.py"),
                   "--server", nc.HOSTS[f["dst"]]["ip"], "--port", str(f["dport"]),
                   "--sport", str(f["sport"]), "--rate", str(f["rate"]),
                   "--size", str(PACKET_SIZE), "--duration", str(f["stop"] - f["start"]),
                   "--name", f["name"], "--start-at", "%.3f" % (t0 + f["start"]),
                   "--print-every", "4"]
            procs.append(net.get(f["src"]).popen(cmd, stdout=hosts_log,
                                                 stderr=subprocess.STDOUT, cwd=HERE))

        info("*** %s / %s: %d s, flows: %s\n" % (
            args.scenario, args.mode, sc["total"],
            ", ".join("%s %s->%s %gMbps [%d-%ds]" % (f["name"], f["src"], f["dst"], f["rate"],
                                                     f["start"], f["stop"])
                      for f in sc["flows"])))
        s1 = net.get("s1")
        snaps = open(os.path.join(outdir, "flowtable_s1.txt"), "w")
        logs.append(snaps)
        pending_snaps = list(sc["snapshots"])
        pending_links = list(sc["link_events"])
        last_print = -1
        while time.time() < t0 + sc["total"] + 1:
            t = time.time() - t0
            if pending_links and t >= pending_links[0]["t"]:
                ev = pending_links.pop(0)
                info("    t=%4.1fs  link %s-%s %s\n" % (t, ev["a"], ev["b"], ev["state"].upper()))
                net.configLinkStatus(ev["a"], ev["b"], ev["state"])
                ev["actual_t"] = t
            if pending_snaps and t >= pending_snaps[0]:
                snaps.write("===== t = %.1f s (%s, %s) =====\n%s\n"
                            % (t, args.scenario, args.mode, s1.cmd("ovs-ofctl dump-flows s1 udp")))
                snaps.flush()
                pending_snaps.pop(0)
            if t >= 0 and int(t) // 5 != last_print:
                last_print = int(t) // 5
                active = [f["name"] for f in sc["flows"] if f["start"] <= t < f["stop"]]
                info("    t=%4.0fs  active flows: %s\n" % (t, ", ".join(active) or "-"))
            time.sleep(0.1)

        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        meta = {"scenario": args.scenario, "mode": args.mode, "t0": t0,
                "total_time": sc["total"], "ping_loss_pct": loss, "packet_size": PACKET_SIZE,
                "link_capacity_mbps": nc.CORE_LINK_BW_MBPS, "flows": sc["flows"],
                "link_events": sc["link_events"], "rate": sc.get("rate"),
                "n_flows": len(sc["flows"])}
        with open(os.path.join(outdir, "scenario.json"), "w") as fh:
            json.dump(meta, fh, indent=2)
        info("*** Done. Results in %s\n" % outdir)
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        for fh in logs:
            fh.close()
        stop_controller(ctrl)      # first, so the shutdown is not logged as link failures
        net.stop()


def main():
    ap = argparse.ArgumentParser(description="Run one congestion / link-failure experiment")
    ap.add_argument("--scenario", choices=["congestion", "linkfail", "sweep", "flows"],
                    default="congestion")
    ap.add_argument("--mode", choices=["static", "adaptive", "app_aware"], required=True)
    ap.add_argument("--rate", type=float, default=None,
                    help="per-flow Mbps (sweep: default 6, flows: default 2)")
    ap.add_argument("--flows", type=int, default=6,
                    help="number of flows (flows scenario only, 1-%d)" % MAX_FLOWS)
    ap.add_argument("--out", default=None, help="output folder")
    ap.add_argument("--userspace", action="store_true",
                    help="OVS userspace datapath (only if the kernel module is missing)")
    ap.add_argument("--no-controller", action="store_true",
                    help="do not start POX (one is already running in the same mode)")
    ap.add_argument("--ctrl", action="append", default=[], metavar="KEY=VALUE",
                    help="extra controller option, e.g. --ctrl threshold=0.7 (repeatable)")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("Mininet needs root:  sudo python3 experiment.py ...")
    if args.rate is None:
        args.rate = 2.0 if args.scenario == "flows" else 6.0
    if not 1 <= args.flows <= MAX_FLOWS:
        sys.exit("--flows must be between 1 and %d" % MAX_FLOWS)
    setLogLevel("info")
    if args.out:
        outdir = os.path.abspath(args.out)
    elif args.scenario == "sweep":
        outdir = os.path.join(HERE, "results", "sweep", "%s_r%g" % (args.mode, args.rate))
    elif args.scenario == "flows":
        outdir = os.path.join(HERE, "results", "flows", "%s_n%d" % (args.mode, args.flows))
    else:
        outdir = os.path.join(HERE, "results", args.scenario, args.mode)
    sys.exit(run(args, outdir))


if __name__ == "__main__":
    main()
