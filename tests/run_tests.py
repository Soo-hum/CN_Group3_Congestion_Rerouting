#!/usr/bin/env python3
"""
run_tests.py - Automated functional test cases, executed in Mininet with the
real POX controller and the real UDP client/server.

    sudo python3 tests/run_tests.py                 # all test cases (~4 minutes)
    sudo python3 tests/run_tests.py --only TC03 TC08
    sudo python3 tests/run_tests.py --list

Every test case gets a fresh network and a fresh controller, so test cases are
independent. Logs of each test case: tests/logs/<TC>/ . A summary table is
written to tests/TEST_REPORT.md.
"""

import argparse
import csv
import os
import subprocess
import sys
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT)

import netconfig as nc  # noqa: E402
from topology import build_network, prepare_hosts  # noqa: E402
from experiment import start_controller, stop_controller  # noqa: E402
from mininet.log import setLogLevel  # noqa: E402

PY = sys.executable
SERVER = os.path.join(ROOT, "app", "udp_server.py")
CLIENT = os.path.join(ROOT, "app", "udp_client.py")
IP = {h: v["ip"] for h, v in nc.HOSTS.items()}


class Env(object):
    """A running network + controller for one test case."""

    def __init__(self, tc, mode, userspace):
        self.tc, self.mode, self.userspace = tc, mode, userspace
        self.dir = os.path.join(TESTS_DIR, "logs", tc)
        os.makedirs(self.dir, exist_ok=True)
        for f in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, f))
        self.procs = []
        self.ctrl = self.net = None

    def __enter__(self):
        os.system("mn -c >/dev/null 2>&1")
        self.ctrl = start_controller(self.mode, self.dir, [])
        if self.ctrl is None:
            raise RuntimeError("controller did not start (see %s/controller.log)" % self.dir)
        self.net = build_network(userspace=self.userspace)
        self.net.start()
        prepare_hosts(self.net, self.userspace)
        if not self.net.waitConnected(timeout=30):
            raise RuntimeError("switches did not connect to the controller")
        return self

    def __exit__(self, *exc):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        stop_controller(self.ctrl)
        if self.net is not None:
            self.net.stop()
        return False

    # -- helpers ---------------------------------------------------------
    def server(self, host, port, *extra):
        p = self.net.get(host).popen([PY, SERVER, "--port", str(port), "--out-dir", self.dir,
                                      "--interval", "0.5"] + list(extra),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
        self.procs.append(p)
        time.sleep(0.7)
        return p

    def client(self, host, dst, port, rate, duration, name, sport=0, *extra):
        p = self.net.get(host).popen([PY, CLIENT, "--server", IP[dst], "--port", str(port),
                                      "--rate", str(rate), "--duration", str(duration),
                                      "--name", name, "--sport", str(sport)] + list(extra),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
        self.procs.append(p)
        return p

    @staticmethod
    def finish(p, timeout=40):
        try:
            out = p.communicate(timeout=timeout)[0]
        except subprocess.TimeoutExpired:
            p.kill()
            out = p.communicate()[0]
        return p.returncode, out.decode(errors="replace")

    @staticmethod
    def stop(p):
        if p.poll() is None:
            p.terminate()
        return Env.finish(p, 10)[1]

    def events(self):
        path = os.path.join(self.dir, "events.csv")
        return list(csv.DictReader(open(path))) if os.path.exists(path) else []

    def flows_s1(self):
        return self.net.get("s1").cmd("ovs-ofctl dump-flows s1")


def summary_loss(out):
    """Loss % from the client's SUMMARY line, or None."""
    for line in out.splitlines():
        if "SUMMARY" in line and "(" in line:
            return float(line.split("(")[1].split("%")[0])
    return None


def out_port(line):
    """'actions=output:3' or 'actions=output:"s1-eth3"' -> 3"""
    act = line.split("actions=")[-1]
    digits = "".join(ch for ch in act.split("output:")[-1] if ch.isdigit())
    if "eth" in act:
        digits = act.split("eth")[-1].strip('"').split(",")[0]
    return int(digits) if digits else None


# =============================================================== test cases
def tc01(env):
    loss = env.net.pingAll(timeout="1")
    ok = loss == 0 and len(env.net.switches) == 5 and len(env.net.hosts) == 4 \
        and len(nc.all_paths("s1", "s5")) >= 3
    return ok, "pingall loss %.0f%%, %d switches, %d hosts, %d paths s1->s5" % (
        loss, len(env.net.switches), len(env.net.hosts), len(nc.all_paths("s1", "s5")))


def tc02(env):
    time.sleep(1)
    res = {}
    for sw in ("s1", "s3", "s5"):
        t = env.net.get(sw).cmd("ovs-ofctl dump-flows %s" % sw)
        res[sw] = (t.count("priority=100,ip"), t.count("priority=200,arp"),
                   t.count("priority=200,udp"), "priority=0 " in t or "priority=0," in t)
    ok = all(r[0] == 4 and r[1] == 1 and r[3] for r in res.values()) \
        and res["s1"][2] == 2 and res["s5"][2] == 2 and res["s3"][2] == 0
    return ok, "; ".join("%s: %d default routes, ARP->ctrl %d, UDP->ctrl %d, drop %s"
                         % (sw, r[0], r[1], r[2], "yes" if r[3] else "no") for sw, r in res.items())


def tc03(env):
    srv = env.server("h3", 5001)
    code, out = env.finish(env.client("h1", "h3", 5001, 2, 4, "T03"))
    env.stop(srv)
    loss = summary_loss(out)
    csv_ok = os.path.exists(os.path.join(env.dir, "flow_T03.csv"))
    ok = code == 0 and loss is not None and loss < 1 and "session accepted" in out and csv_ok
    return ok, "client exit %d, handshake %s, SUMMARY loss %s%%, server CSV %s" % (
        code, "ok" if "session accepted" in out else "missing", loss, "written" if csv_ok else "missing")


def tc04(env):
    srv = env.server("h4", 5002)
    cs = [env.client("h1", "h4", 5002, 1.5, 5, "T04a", 41001),
          env.client("h2", "h4", 5002, 1.5, 5, "T04b", 41002),
          env.client("h1", "h4", 5002, 1.5, 5, "T04c", 41003)]
    res = [env.finish(c) for c in cs]
    slog = env.stop(srv)
    losses = [summary_loss(o) for _, o in res]
    ok = all(c == 0 for c, _ in res) and all(l is not None and l < 1 for l in losses) \
        and "(3 active)" in slog
    return ok, "3 clients -> one server: exit codes %s, losses %s %%, server had 3 sessions " \
               "at once: %s" % ([c for c, _ in res], losses, "(3 active)" in slog)


def tc05(env):
    srv = env.server("h3", 5001, "--max-sessions", "2")
    c1 = env.client("h1", "h3", 5001, 1, 5, "T05a", 41011)
    c2 = env.client("h2", "h3", 5001, 1, 5, "T05b", 41012)
    time.sleep(1.0)
    code3, out3 = env.finish(env.client("h1", "h3", 5001, 1, 3, "T05c", 41013, "--retries", "2"))
    code1, _ = env.finish(c1)
    code2, _ = env.finish(c2)
    slog = env.stop(srv)
    ok = code3 == 3 and "REJECTED" in out3 and code1 == 0 and code2 == 0 and "REJECT" in slog
    return ok, "max 2 sessions: clients 1-2 exit %d/%d, client 3 exit %d (%s)" % (
        code1, code2, code3, "REJECTED" if "REJECTED" in out3 else "not rejected")


def tc06(env):
    code_a, out_a = env.finish(env.client("h1", "h3", 5999, 1, 2, "T06a", 0,
                                          "--retries", "2", "--timeout", "0.5"))
    srv = env.server("h3", 5001)
    env.net.get("h2").cmd("%s -c \"import socket; s=socket.socket(socket.AF_INET, "
                          "socket.SOCK_DGRAM); s.sendto(b'not-our-protocol', ('%s', 5001))\""
                          % (PY, IP["h3"]))
    time.sleep(0.5)
    code_b, out_b = env.finish(env.client("h1", "h3", 5001, 1, 2, "T06b"))
    slog = env.stop(srv)
    ok = code_a == 2 and "unreachable" in out_a and "malformed" in slog and code_b == 0
    return ok, "no server -> client exit %d (%s); malformed datagram %s; next client exit %d" % (
        code_a, "unreachable detected" if "unreachable" in out_a else "?",
        "ignored by server" if "malformed" in slog else "NOT logged", code_b)


def tc07(env):
    srv = env.server("h3", 5001)
    c = env.client("h1", "h3", 5001, 1, 3, "T07", 42007)
    time.sleep(1.5)
    table = env.flows_s1()
    env.finish(c)
    env.stop(srv)
    line = next((l for l in table.splitlines() if "tp_src=42007" in l), "")
    port = out_port(line) if line else None
    ok = "priority=300" in line and port == nc.port_map()[("s1", "s2")]
    return ok, "per-flow rule on s1: %s, output port %s (P1 = port %d)" % (
        "found" if line else "missing", port, nc.port_map()[("s1", "s2")])


def congestion_traffic(env):
    s3, s4 = env.server("h3", 5001), env.server("h4", 5002)
    a = env.client("h1", "h3", 5001, 6, 16, "A", 40001)
    time.sleep(4)
    t_b = time.time()
    b = env.client("h2", "h4", 5002, 6, 11, "B", 40002)
    time.sleep(5)
    table = env.flows_s1()
    ra, rb = env.finish(a), env.finish(b)
    env.stop(s3)
    env.stop(s4)
    return t_b, table, summary_loss(ra[1]), summary_loss(rb[1])


def tc08(env):
    t_b, table, la, lb = congestion_traffic(env)
    ev = env.events()
    det = [e for e in ev if e["event"] == "CONGESTION_DETECTED"]
    rr = [e for e in ev if e["event"] == "REROUTED"]
    react = float(rr[0]["time"]) - t_b if rr else None
    ports = {out_port(l) for l in table.splitlines() if "priority=300" in l and "tp_src=4000" in l}
    ok = bool(det) and bool(rr) and react is not None and react < 4 and la is not None \
        and lb is not None and la < 5 and lb < 5 and len(ports) == 2
    return ok, "detected %s, rerouted %s after %.1fs (%s), loss A %s%% B %s%%, flows use ports %s" % (
        bool(det), rr[0]["flow"] if rr else "-", react or -1,
        rr[0]["to_path"] if rr else "-", la, lb, sorted(p for p in ports if p))


def tc09(env):
    t_b, table, la, lb = congestion_traffic(env)
    ev = env.events()
    det = any(e["event"] == "CONGESTION_DETECTED" for e in ev)
    rr = any(e["event"] == "REROUTED" for e in ev)
    # the 1 s switch queue absorbs the first seconds of overload, so the exact loss
    # varies per run; what matters is clear loss and no rerouting
    ok = det and not rr and la is not None and lb is not None and (la + lb) > 5
    return ok, "static: detected %s, rerouted %s, loss A %s%% B %s%% (expected clear loss " \
               "because nothing is rerouted)" % (det, rr, la, lb)


def tc10(env):
    srv = env.server("h3", 5001)
    c = env.client("h1", "h3", 5001, 3, 12, "T10", 40010)
    time.sleep(4)
    env.net.configLinkStatus("s1", "s2", "down")
    time.sleep(1)
    ping = env.net.ping([env.net.get("h2"), env.net.get("h4")], timeout="1")
    time.sleep(3)
    env.net.configLinkStatus("s1", "s2", "up")
    code, out = env.finish(c)
    env.stop(srv)
    ev = env.events()
    down = any(e["event"] == "LINK_DOWN" for e in ev)
    moved = [e for e in ev if e["event"] == "REROUTED" and "link-failure" in e["detail"]]
    loss = summary_loss(out)
    ok = down and moved and loss is not None and loss < 5 and ping == 0
    return ok, "LINK_DOWN %s, flows moved %d (%s), client loss %s%%, ping during failure %.0f%% loss" % (
        down, len(moved), moved[0]["to_path"] if moved else "-", loss, ping)


def tc11(env):
    srv = env.server("h3", 5001)
    code, out = env.finish(env.client("h1", "h3", 5001, 2, 4, "T11", 40011))
    env.stop(srv)
    time.sleep(0.5)
    ev = env.events()
    start = any(e["event"] == "APP_SESSION_START" and "name=T11" in e["detail"] for e in ev)
    end = any(e["event"] == "APP_SESSION_END" and "name=T11" in e["detail"] for e in ev)
    qos_rows = [r for r in csv.DictReader(open(os.path.join(env.dir, "app_qos.csv")))
                if r["name"] == "T11"]
    named = any("(T11)" in e["flow"] for e in ev)
    ok = start and end and len(qos_rows) >= 4 and named
    return ok, "controller got session start %s, %d QoS reports, session end %s, flow " \
               "labelled with app name %s" % (start, len(qos_rows), end, named)


def tc12(env):
    srv = env.server("h3", 5001)
    c = env.client("h1", "h3", 5001, 2, 12, "T12", 40012, "--print-every", "1")
    time.sleep(4)
    env.stop(srv)                                    # service failure
    time.sleep(2)
    srv2 = env.server("h3", 5001)                    # service comes back
    code, out = env.finish(c)
    slog2 = env.stop(srv2)
    ok = code == 0 and "HELLO again" in out and "session T12" in slog2 and "SUMMARY" in out
    return ok, "server killed for 2 s: client exit %d, re-handshake %s, new server took the " \
               "session %s" % (code, "HELLO again" in out, "session T12" in slog2)


TESTS = [
    ("TC01", "Topology and connectivity (pingall)", "Mininet + SDN", "app_aware", tc01),
    ("TC02", "Controller installs base flow rules", "Mininet + SDN", "app_aware", tc02),
    ("TC03", "UDP client-server session (HELLO/DATA/BYE)", "Socket implementation", "app_aware", tc03),
    ("TC04", "Concurrent clients on one server", "Socket implementation", "app_aware", tc04),
    ("TC05", "Server overload: extra client rejected", "Robustness", "app_aware", tc05),
    ("TC06", "Error handling: no server / malformed packet", "Socket implementation", "app_aware", tc06),
    ("TC07", "Per-flow rule installed on default path P1", "Socket-SDN integration", "app_aware", tc07),
    ("TC08", "Congestion detected and flow rerouted", "Dynamic SDN", "app_aware", tc08),
    ("TC09", "Static baseline: congestion, no rerouting", "Performance baseline", "static", tc09),
    ("TC10", "Link failure: flows moved, ping still works", "Robustness", "app_aware", tc10),
    ("TC11", "Application QoS feedback reaches controller", "Socket-SDN integration", "app_aware", tc11),
    ("TC12", "Service failure: server restart, client recovers", "Robustness", "app_aware", tc12),
]


def main():
    ap = argparse.ArgumentParser(description="Functional test cases in Mininet")
    ap.add_argument("--only", nargs="*", help="run only these test IDs, e.g. TC03 TC08")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--userspace", action="store_true",
                    help="OVS userspace datapath (only if the kernel module is missing)")
    args = ap.parse_args()
    if args.list:
        for t in TESTS:
            print("%s  %-50s [%s]" % t[:3])
        return
    if os.geteuid() != 0:
        sys.exit("Mininet needs root:  sudo python3 tests/run_tests.py")
    setLogLevel("warning")
    selected = [t for t in TESTS if not args.only or t[0] in args.only]
    results = []
    for tc, title, area, mode, fn in selected:
        print("%s  %-50s ..." % (tc, title), end=" ", flush=True)
        t0 = time.time()
        try:
            with Env(tc, mode, args.userspace) as env:
                ok, details = fn(env)
        except Exception as e:                           # a crash is a failed test
            ok, details = False, "error: %s" % e
        dt = time.time() - t0
        results.append((tc, title, area, mode, ok, details, dt))
        print("%s (%.0fs)\n      %s" % ("PASS" if ok else "FAIL", dt, details), flush=True)
    os.system("mn -c >/dev/null 2>&1")

    passed = sum(1 for r in results if r[4])
    lines = ["# Test report", "",
             "Generated by `sudo python3 tests/run_tests.py` on %s. Each test case runs in a fresh "
             "Mininet network with the POX controller and the UDP client/server application. "
             "Logs: `tests/logs/<TC>/`." % time.strftime("%Y-%m-%d %H:%M"), "",
             "**%d of %d test cases passed.**" % (passed, len(results)), "",
             "| ID | Test case | Rubric area | Controller mode | Result | Observed |",
             "|---|---|---|---|---|---|"]
    for tc, title, area, mode, ok, details, dt in results:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (tc, title, area, mode,
                                                         "PASS" if ok else "**FAIL**", details))
    with open(os.path.join(TESTS_DIR, "TEST_REPORT.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n%d/%d passed - report: tests/TEST_REPORT.md" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
