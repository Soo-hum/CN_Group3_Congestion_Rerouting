#!/usr/bin/env python3
"""
udp_server.py - Concurrent UDP measurement server (runs on a Mininet host).

One UDP socket serves many clients at the same time. Each client is a
SESSION (identified by client address + session id) with its own statistics.

Threads
    main thread      receives datagrams and dispatches them by message type
    reporter thread  every `interval` seconds: closes the statistics interval of
                     every session, sends a REPORT to the client, writes one CSV
                     row, sends a QoS report to the SDN controller, and expires
                     idle sessions
    (a lock protects the session table, which both threads use)

Packet-level view (sequence numbers and timestamps)
    Every DATA packet carries a sequence number and the sender's timestamp.
    --show-packets [N]  prints every Nth packet (seq, send time, receive time,
                        one-way delay); gaps (lost packets), late (out-of-order)
                        and duplicate packets are always printed
    --trace             writes trace_<flow>.csv with one row per packet

Socket-SDN integration
    The server tells the SDN controller what the APPLICATION sees: when a
    session starts (with the rate the client asked for), its QoS every
    interval (throughput / loss / delay / jitter) and when it ends. The
    messages are JSON over a UNIX datagram socket (path in netconfig.py).
    This works across Mininet hosts because they share the file system.

Example (from the Mininet CLI):
    h3 python3 app/udp_server.py --port 5001 --out-dir /tmp/demo &
    h4 python3 app/udp_server.py --port 5003 --out-dir /tmp/demo --show-packets > /tmp/pkts.log 2>&1 &
"""

import argparse
import csv
import json
import os
import signal
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import protocol as P  # noqa: E402

try:
    from netconfig import FEEDBACK_SOCKET as DEFAULT_FEEDBACK  # noqa: E402
except ImportError:                                            # pragma: no cover
    DEFAULT_FEEDBACK = "/tmp/sdn_app_feedback.sock"


def log(msg):
    print("[server %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Session(object):
    """Statistics for one client session."""

    def __init__(self, sid, addr, hello, server_ip, server_port, csv_path, now,
                 trace_path=None, show_every=0):
        self.sid = sid
        self.addr = addr
        self.name = hello["name"] or "%s:%d" % addr
        self.rate = hello["rate_mbps"]
        self.size = hello["size"]
        self.server_ip, self.server_port = server_ip, server_port
        self.created = self.last_seen = now
        self.total_sent = None
        # cumulative
        self.received = 0
        self.bytes = 0
        self.delay_sum = 0.0
        self.lost_total = 0
        self.max_seq = -1
        self.jitter = 0.0
        self.prev_transit = None
        # current interval
        self.i_pkts = self.i_bytes = 0
        self.i_delay = 0.0
        self.i_max_seq = -1
        self.reported_seq = -1
        new = not os.path.exists(csv_path)
        self.csv_file = open(csv_path, "a", newline="")
        self.csv = csv.writer(self.csv_file)
        if new:
            self.csv.writerow(["time", "label", "throughput_mbps", "packets", "lost",
                               "loss_pct", "avg_delay_ms", "jitter_ms"])
        # packet-level view
        self.seqs = P.SeqTracker()
        self.show_every = show_every
        self._event_t, self._suppressed = 0.0, 0
        self.trace_file = self.trace = None
        if trace_path:
            new = not os.path.exists(trace_path)
            self.trace_file = open(trace_path, "a", newline="")
            self.trace = csv.writer(self.trace_file)
            if new:
                self.trace.writerow(["recv_time", "seq", "send_time", "delay_ms", "status",
                                     "missing_before"])

    def flow_tuple(self):
        """(client_ip, client_port, server_ip, server_port) - the flow the controller sees."""
        return [self.addr[0], self.addr[1], self.server_ip, self.server_port]

    def on_data(self, msg, now):
        transit = now - msg.timestamp
        status, first, n_missing = self.seqs.update(msg.seq)
        if self.trace is not None:
            self.trace.writerow(["%.6f" % now, msg.seq, "%.6f" % msg.timestamp,
                                 "%.3f" % (1000 * transit), status, n_missing])
        if self.show_every:
            self.show_packet(msg, now, transit, status, first, n_missing)
        if self.prev_transit is not None:
            self.jitter += (abs(transit - self.prev_transit) - self.jitter) / 16.0
        self.prev_transit = transit
        self.received += 1
        self.bytes += msg.size
        self.delay_sum += transit
        self.max_seq = max(self.max_seq, msg.seq)
        self.i_pkts += 1
        self.i_bytes += msg.size
        self.i_delay += transit
        self.i_max_seq = max(self.i_max_seq, msg.seq)
        self.last_seen = now

    def show_packet(self, msg, now, transit, status, first, n):
        """One line per packet: seq, send time (from the header), receive time, delay."""
        note = ""
        if status == "gap":
            span = "seq %d" % first if n == 1 else "seq %d-%d" % (first, first + n - 1)
            note = "  <- GAP: %s missing (%d lost)" % (span, n)
        elif status == "late":
            note = "  <- LATE: arrived after seq %d (out of order)" % self.seqs.max_seq
        elif status == "dup":
            note = "  <- DUPLICATE"
        if note:                       # always shown, but at most 4 lines per second
            if now - self._event_t < 0.25:
                self._suppressed += 1
                return
            if self._suppressed:
                note += "  (+%d more not shown)" % self._suppressed
            self._event_t, self._suppressed = now, 0
        elif msg.seq % self.show_every:
            return
        print("[pkt] %s  seq=%-7d sent=%s  recv=%s  delay=%6.2f ms%s"
              % (self.name, msg.seq, P.fmt_time(msg.timestamp), P.fmt_time(now),
                 1000 * transit, note), flush=True)

    def close_interval(self, t_end, interval):
        """Finish the current interval: returns the stats dict and writes a CSV row."""
        lost = 0
        if self.i_pkts:
            lost = max(0, (self.i_max_seq - self.reported_seq) - self.i_pkts)
            self.reported_seq = max(self.reported_seq, self.i_max_seq)
        self.lost_total += lost
        sent_est = self.i_pkts + lost
        stats = {
            "received": self.i_pkts, "lost": lost,
            "throughput_mbps": self.i_bytes * 8 / interval / 1e6,
            "loss_pct": 100.0 * lost / sent_est if sent_est else 0.0,
            "delay_ms": 1000 * self.i_delay / self.i_pkts if self.i_pkts else 0.0,
            "jitter_ms": 1000 * self.jitter,
        }
        self.csv.writerow(["%.3f" % t_end, self.name, "%.3f" % stats["throughput_mbps"],
                           self.i_pkts, lost, "%.2f" % stats["loss_pct"],
                           ("%.3f" % stats["delay_ms"]) if self.i_pkts else "",
                           "%.3f" % stats["jitter_ms"]])
        self.csv_file.flush()
        if self.trace_file is not None:
            self.trace_file.flush()
        self.i_pkts = self.i_bytes = 0
        self.i_delay = 0.0
        self.i_max_seq = -1
        return stats

    def summary(self):
        sent = self.total_sent if self.total_sent is not None else self.received + self.lost_total
        lost = max(0, sent - self.received)
        dur = max(self.last_seen - self.created, 1e-6)
        return (sent, self.received, lost, 100.0 * lost / sent if sent else 0.0,
                1000 * self.delay_sum / self.received if self.received else 0.0,
                self.bytes * 8 / dur / 1e6)

    def close(self):
        for fh in (self.csv_file, self.trace_file):
            try:
                if fh is not None:
                    fh.close()
            except OSError:
                pass


class UDPServer(object):

    def __init__(self, args):
        self.args = args
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.bind((args.bind, args.port))
        self.sock.settimeout(0.2)
        self.sessions = {}          # (addr, sid) -> Session
        self.finished = {}          # (addr, sid) -> (summary tuple, reason)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.counters = {"malformed": 0, "rejected": 0, "unknown": 0, "sessions": 0}
        self._last_error_sent = {}
        self._resume_info = {}      # (addr, sid) -> HELLO body, to resume timed-out sessions
        self.feedback = None
        self._feedback_warned = False
        if args.feedback and args.feedback.lower() != "none":
            self.feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        os.makedirs(args.out_dir, exist_ok=True)

    # ---------------------------------------------------------- helpers
    def send(self, data, addr):
        try:
            self.sock.sendto(data, addr)
        except OSError as e:               # e.g. network unreachable during a link failure
            log("send to %s:%d failed: %s" % (addr[0], addr[1], e))

    def notify_controller(self, msg):
        """Socket-SDN integration: tell the controller what the application observes."""
        if self.feedback is None:
            return
        try:
            self.feedback.sendto(json.dumps(msg).encode(), self.args.feedback)
        except OSError as e:
            if not self._feedback_warned:
                log("controller feedback channel unavailable (%s) - continuing without it" % e)
                self._feedback_warned = True

    def local_ip_for(self, addr):
        """Our own IP address as the client sees it (needed to name the flow)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(addr)
            return s.getsockname()[0]
        finally:
            s.close()

    # ---------------------------------------------------------- dispatch
    def handle(self, data, addr, now):
        try:
            msg = P.unpack(data)
        except P.ProtocolError as e:
            self.counters["malformed"] += 1
            if self.counters["malformed"] <= 5:
                log("ignored malformed datagram from %s:%d (%s)" % (addr[0], addr[1], e))
            self.send(P.pack(P.ERROR, 0, body=(P.ERR_MALFORMED,)), addr)
            return
        key = (addr, msg.session_id)
        with self.lock:
            if msg.type == P.HELLO:
                self.on_hello(key, msg, now)
            elif msg.type == P.DATA:
                s = self.sessions.get(key)
                if s is not None:
                    s.on_data(msg, now)
                elif key in self.finished and self.finished[key][1] == "timeout":
                    log("session %s resumed after idle timeout" % self.finished[key][0])
                    del self.finished[key]
                    self.on_hello(key, None, now, resume=msg)
                else:
                    self.on_unknown(addr, msg.session_id, now)
            elif msg.type == P.BYE:
                self.on_bye(key, msg, now)
            else:
                log("unexpected %s from %s:%d" % (P.TYPE_NAMES[msg.type], addr[0], addr[1]))

    def on_hello(self, key, msg, now, resume=None):
        addr, sid = key
        if key in self.sessions:                         # retransmitted HELLO
            self.send(P.pack(P.HELLO_ACK, sid, body=(int(self.args.interval * 1000),
                                                       self.args.max_sessions)), addr)
            return
        if len(self.sessions) >= self.args.max_sessions:
            self.counters["rejected"] += 1
            log("REJECT %s:%d - server full (%d sessions)" % (addr[0], addr[1], len(self.sessions)))
            self.send(P.pack(P.REJECT, sid, body=(P.REJECT_SERVER_FULL,)), addr)
            return
        hello = msg.body if msg else self._resume_info.pop(key, None) or \
            {"name": "", "rate_mbps": 0.0, "size": resume.size, "duration": 0.0}
        name = hello["name"] or "%s_%d" % (addr[0], addr[1])
        csv_path = os.path.join(self.args.out_dir, "flow_%s.csv" % name)
        trace_path = os.path.join(self.args.out_dir, "trace_%s.csv" % name) if self.args.trace else None
        s = Session(sid, addr, hello, self.local_ip_for(addr), self.args.port, csv_path, now,
                    trace_path, self.args.show_packets)
        self.sessions[key] = s
        self._resume_info[key] = hello
        self.counters["sessions"] += 1
        if resume is not None:
            s.on_data(resume, now)
        else:
            self.send(P.pack(P.HELLO_ACK, sid, body=(int(self.args.interval * 1000),
                                                       self.args.max_sessions)), addr)
        log("session %s from %s:%d started: %s Mbps, %d-byte packets (%d active)"
            % (s.name, addr[0], addr[1], ("%.1f" if s.rate >= 1 else "%.2f") % s.rate, s.size,
               len(self.sessions)))
        self.notify_controller({"type": "session_start", "name": s.name, "t": now,
                                "flow": s.flow_tuple(), "rate_mbps": s.rate, "size": s.size})

    def on_bye(self, key, msg, now):
        addr, sid = key
        s = self.sessions.pop(key, None)
        if s is None:
            if key in self.finished:                     # retransmitted BYE: resend summary
                self.send(P.pack(P.SUMMARY, sid, body=self.finished[key][0]), addr)
            else:
                self.on_unknown(addr, sid, now)
            return
        s.total_sent = msg.body["total_sent"]
        summ = s.summary()
        self.finished[key] = (summ, "bye")
        self.send(P.pack(P.SUMMARY, sid, body=summ), addr)
        s.close()
        log("session %s finished: sent %d, received %d, lost %d (%.2f%%), avg delay %.1f ms, "
            "out of order %d, duplicates %d" % (s.name, summ[0], summ[1], summ[2], summ[3],
                                                 summ[4], s.seqs.reordered, s.seqs.duplicates))
        self.notify_controller({"type": "session_end", "name": s.name, "t": now,
                                "flow": s.flow_tuple(), "reason": "bye", "loss_pct": summ[3]})

    def on_unknown(self, addr, sid, now):
        self.counters["unknown"] += 1
        if now - self._last_error_sent.get(addr, 0) >= 1.0:   # at most 1 ERROR/s per client
            self._last_error_sent[addr] = now
            self.send(P.pack(P.ERROR, sid, body=(P.ERR_UNKNOWN_SESSION,)), addr)

    # ---------------------------------------------------------- reporter thread
    def reporter(self):
        iv = self.args.interval
        while not self.stop.is_set():
            boundary = (int(time.time() / iv) + 1) * iv
            self.stop.wait(max(0.0, boundary - time.time()))
            now = time.time()
            with self.lock:
                for key, s in list(self.sessions.items()):
                    st = s.close_interval(boundary, iv)
                    self.send(P.pack(P.REPORT, s.sid, body=(
                        st["received"], st["lost"], st["throughput_mbps"], st["loss_pct"],
                        st["delay_ms"], st["jitter_ms"])), s.addr)
                    self.notify_controller(dict(type="qos", name=s.name, t=boundary,
                                                flow=s.flow_tuple(), rate_mbps=s.rate, **st))
                    if now - s.last_seen > self.args.session_timeout:
                        del self.sessions[key]
                        self.finished[key] = (s.summary(), "timeout")
                        s.close()
                        log("session %s timed out (no packets for %.0f s)"
                            % (s.name, self.args.session_timeout))
                        self.notify_controller({"type": "session_end", "name": s.name, "t": now,
                                                "flow": s.flow_tuple(), "reason": "timeout"})

    # ---------------------------------------------------------- main loop
    def serve(self):
        t = threading.Thread(target=self.reporter, name="reporter", daemon=True)
        t.start()
        end = time.time() + self.args.duration if self.args.duration > 0 else float("inf")
        log("listening on UDP %s:%d (max %d sessions, report every %.1fs, feedback=%s)"
            % (self.args.bind, self.args.port, self.args.max_sessions, self.args.interval,
               self.args.feedback if self.feedback else "off"))
        if self.args.show_packets:
            n = self.args.show_packets
            which = "every DATA packet" if n == 1 else "every %dth DATA packet" % n
            log("packet view: printing %s with its seq and timestamps (gaps, late and "
                "duplicate packets are always printed)" % which)
        if self.args.trace:
            log("per-packet trace: %s/trace_<flow>.csv" % self.args.out_dir)
        try:
            while not self.stop.is_set() and time.time() < end:
                try:
                    data, addr = self.sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as e:
                    log("receive error: %s" % e)
                    continue
                self.handle(data, addr, time.time())
        finally:
            self.stop.set()
            t.join(timeout=2)
            with self.lock:
                for s in self.sessions.values():
                    s.close()
            log("stopped. sessions served: %d, rejected: %d, malformed datagrams: %d, "
                "packets for unknown sessions: %d" % (self.counters["sessions"],
                                                      self.counters["rejected"],
                                                      self.counters["malformed"],
                                                      self.counters["unknown"]))


def main():
    ap = argparse.ArgumentParser(description="Concurrent UDP measurement server")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--interval", type=float, default=0.5, help="report/statistics interval (s)")
    ap.add_argument("--duration", type=float, default=0, help="seconds to run (0 = until Ctrl+C)")
    ap.add_argument("--max-sessions", type=int, default=8, help="reject clients beyond this")
    ap.add_argument("--session-timeout", type=float, default=15.0,
                    help="close a session after this many seconds without packets")
    ap.add_argument("--out-dir", default=".", help="where flow_<name>.csv files are written")
    ap.add_argument("--show-packets", type=int, nargs="?", const=1, default=0, metavar="N",
                    help="print every Nth DATA packet with its seq and timestamps "
                         "(no N = every packet)")
    ap.add_argument("--trace", action="store_true",
                    help="write trace_<flow>.csv: one row per packet (seq, times, delay, status)")
    ap.add_argument("--feedback", default=DEFAULT_FEEDBACK,
                    help="UNIX socket of the SDN controller ('none' to disable)")
    args = ap.parse_args()
    server = UDPServer(args)
    signal.signal(signal.SIGTERM, lambda *_: server.stop.set())
    try:
        server.serve()
    except KeyboardInterrupt:
        server.stop.set()


if __name__ == "__main__":
    main()
