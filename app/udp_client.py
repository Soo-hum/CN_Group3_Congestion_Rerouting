#!/usr/bin/env python3
"""
udp_client.py - UDP streaming client (runs on a Mininet host).

Life of a session
    1. HELLO      -> server  (asks for a session: rate, packet size, duration, name)
       HELLO_ACK  <- server  (or REJECT if the server is full). HELLO is retried
                             because UDP may lose it.
    2. DATA       -> server  constant bit rate, each packet carries seq + send time
       REPORT     <- server  every interval: what the server actually received
    3. BYE        -> server  (with the number of packets sent; retried)
       SUMMARY    <- server  final loss / delay / throughput

Threads: the main thread paces and sends DATA; a receiver thread handles
REPORT / ERROR / SUMMARY messages at the same time.

Error handling
    * server not running   -> ICMP "port unreachable" is noticed (connected UDP
                              socket), HELLO retried, then exit code 2
    * server full          -> REJECT received, exit code 3
    * server restarted     -> server answers ERROR(unknown session), the client
                              sends HELLO again and continues the same stream
    * send errors          -> counted, sending continues (e.g. during a link failure)

Example (from the Mininet CLI):
    h1 python3 app/udp_client.py --server 10.0.0.3 --port 5001 --rate 6 --duration 30 --name A &
"""

import argparse
import os
import random
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import protocol as P  # noqa: E402

EXIT_OK, EXIT_UNREACHABLE, EXIT_REJECTED, EXIT_NO_SUMMARY = 0, 2, 3, 4


class Client(object):

    def __init__(self, a):
        self.a = a
        self.tag = "[client %s]" % a.name
        self.sid = random.getrandbits(32)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        if a.sport:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("0.0.0.0", a.sport))
        # "connected" UDP socket: only accepts datagrams from the server and
        # reports ICMP errors (e.g. port unreachable) as ConnectionRefusedError
        self.sock.connect((a.server, a.port))
        self.acked = threading.Event()
        self.rejected = threading.Event()
        self.summary = None
        self.summary_evt = threading.Event()
        self.need_rehello = threading.Event()
        self.stop = threading.Event()
        self.reports = 0
        self.icmp_errors = 0
        self.send_errors = 0
        self.last_report = None

    def log(self, msg):
        print("%s %s" % (self.tag, msg), flush=True)

    def hello_packet(self):
        return P.pack(P.HELLO, self.sid, body=(self.a.rate, self.a.size, self.a.duration,
                                               self.a.name))

    # ------------------------------------------------------------ receiver thread
    def receiver(self):
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                data = self.sock.recv(65535)
            except socket.timeout:
                continue
            except ConnectionRefusedError:
                self.icmp_errors += 1
                continue
            except OSError:
                continue
            try:
                msg = P.unpack(data)
            except P.ProtocolError as e:
                self.log("ignored bad datagram from server: %s" % e)
                continue
            if msg.session_id != self.sid and msg.type != P.ERROR:
                continue
            if msg.type == P.HELLO_ACK:
                if not self.acked.is_set():
                    self.log("session accepted (reports every %d ms)"
                             % msg.body["report_interval_ms"])
                self.acked.set()
            elif msg.type == P.REJECT:
                self.rejected.set()
            elif msg.type == P.REPORT:
                self.reports += 1
                self.last_report = msg.body
                if not self.a.quiet and self.reports % self.a.print_every == 0:
                    b = msg.body
                    self.log("server sees %.2f Mbps, loss %.1f%%, delay %.1f ms, jitter %.2f ms"
                             % (b["throughput_mbps"], b["loss_pct"], b["delay_ms"], b["jitter_ms"]))
            elif msg.type == P.ERROR:
                if msg.body["code"] == P.ERR_UNKNOWN_SESSION:
                    self.need_rehello.set()
            elif msg.type == P.SUMMARY:
                self.summary = msg.body
                self.summary_evt.set()

    # ------------------------------------------------------------ phases
    def safe_send(self, data):
        try:
            self.sock.send(data)
            return True
        except ConnectionRefusedError:
            self.icmp_errors += 1
        except OSError:
            self.send_errors += 1
        return False

    def handshake(self):
        for attempt in range(1, self.a.retries + 1):
            self.safe_send(self.hello_packet())
            if self.acked.wait(self.a.timeout):
                return EXIT_OK
            if self.rejected.is_set():
                self.log("REJECTED by server (server full)")
                return EXIT_REJECTED
            self.log("no answer to HELLO (attempt %d/%d)%s" % (
                attempt, self.a.retries,
                " - server port unreachable" if self.icmp_errors else ""))
        self.log("server %s:%d unreachable - giving up" % (self.a.server, self.a.port))
        return EXIT_UNREACHABLE

    def stream(self):
        a = self.a
        gap = (a.size * 8.0) / (a.rate * 1e6)
        start = time.time()
        end = start + a.duration
        next_send = start
        seq = 0
        last_rehello = 0.0
        self.log("streaming %.2f Mbps (%d-byte packets, %.0f pkt/s) for %.1fs to %s:%d"
                 % (a.rate, a.size, 1.0 / gap, a.duration, a.server, a.port))
        while True:
            now = time.time()
            if now >= end:
                break
            if self.need_rehello.is_set() and now - last_rehello >= 1.0:
                # the server lost our session (e.g. it was restarted): ask again
                self.need_rehello.clear()
                last_rehello = now
                self.log("server does not know this session - sending HELLO again")
                self.safe_send(self.hello_packet())
            if now < next_send:
                time.sleep(min(next_send - now, 0.002))
                continue
            burst = 0
            while next_send <= now and burst < 50:
                self.safe_send(P.pack(P.DATA, self.sid, seq=seq, size=a.size))
                seq += 1
                burst += 1
                next_send += gap
        return seq, time.time() - start

    def finish(self, sent):
        for _ in range(self.a.retries):
            self.safe_send(P.pack(P.BYE, self.sid, body=(sent,)))
            if self.summary_evt.wait(self.a.timeout):
                s = self.summary
                self.log("SUMMARY: sent %d, server received %d, lost %d (%.2f%%), "
                         "avg delay %.1f ms, avg throughput %.2f Mbps"
                         % (s["total_sent"], s["received"], s["lost"], s["loss_pct"],
                            s["avg_delay_ms"], s["avg_throughput_mbps"]))
                return EXIT_OK
        self.log("no SUMMARY from server (sent %d packets)" % sent)
        return EXIT_NO_SUMMARY

    def run(self):
        if self.a.start_at > time.time():
            time.sleep(self.a.start_at - time.time())
        rx = threading.Thread(target=self.receiver, name="receiver", daemon=True)
        rx.start()
        try:
            code = self.handshake()
            if code != EXIT_OK:
                return code
            sent, elapsed = self.stream()
            self.log("done sending: %d packets in %.1fs (%.2f Mbps offered), "
                     "send errors %d, ICMP errors %d"
                     % (sent, elapsed, sent * self.a.size * 8 / elapsed / 1e6,
                        self.send_errors, self.icmp_errors))
            return self.finish(sent)
        finally:
            self.stop.set()
            rx.join(timeout=1)
            self.sock.close()


def main():
    ap = argparse.ArgumentParser(description="UDP streaming client")
    ap.add_argument("--server", required=True, help="server IP")
    ap.add_argument("--port", type=int, required=True, help="server UDP port")
    ap.add_argument("--sport", type=int, default=0, help="client UDP port (0 = any)")
    ap.add_argument("--rate", type=float, default=5.0, help="Mbps of UDP payload")
    ap.add_argument("--size", type=int, default=1200, help="datagram size in bytes")
    ap.add_argument("--duration", type=float, default=30.0, help="seconds to stream")
    ap.add_argument("--name", default="flow", help="flow name (shown in server logs/CSV)")
    ap.add_argument("--start-at", type=float, default=0.0, help="absolute epoch start time")
    ap.add_argument("--retries", type=int, default=5, help="HELLO/BYE retries")
    ap.add_argument("--timeout", type=float, default=1.0, help="seconds to wait per retry")
    ap.add_argument("--print-every", type=int, default=2, help="print every Nth REPORT")
    ap.add_argument("--quiet", action="store_true", help="do not print REPORTs")
    a = ap.parse_args()
    if a.size < P.MIN_DATA_SIZE:
        sys.exit("--size must be at least %d bytes" % P.MIN_DATA_SIZE)
    if a.rate <= 0 or a.duration <= 0:
        sys.exit("--rate and --duration must be positive")
    try:
        sys.exit(Client(a).run())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
