#!/usr/bin/env python3
"""
packet_sniffer.py - Shows our protocol ON THE WIRE: captures packets with a raw
socket (AF_PACKET) and decodes the 20-byte header of every datagram, including
the sequence number and the sender's timestamp.

Run it on a Mininet host (needs root, which the Mininet CLI already has):

    mininet> h3 python3 tools/packet_sniffer.py --port 5001 --count 12
    mininet> h3 python3 tools/packet_sniffer.py --port 5001 --count 400 --every 50
    mininet> h3 python3 tools/packet_sniffer.py --port 5001 --count 200 --write /tmp/h3.pcap

It can also decode a capture file made by this tool, tcpdump or Wireshark
(pcap format; in Wireshark use "Save As... pcap"):

    python3 tools/packet_sniffer.py --read /tmp/h3.pcap

Columns: capture time, source -> destination, message type, session id,
sequence number, sender timestamp (from the header), one-way delay
(capture time - sender timestamp; Mininet hosts share one clock) and notes
(gaps in the sequence numbers = lost packets, late = out of order).
"""

import argparse
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))
import protocol as P  # noqa: E402

ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4


# ------------------------------------------------------------------ parsing
def parse_ip(pkt):
    """IPv4 packet -> (src, sport, dst, dport, udp_payload) or None."""
    if len(pkt) < 20 or pkt[0] >> 4 != 4 or pkt[9] != 17:
        return None
    ihl = (pkt[0] & 0x0F) * 4
    if struct.unpack("!H", pkt[6:8])[0] & 0x1FFF:          # not the first fragment
        return None
    sport, dport, ulen = struct.unpack("!HHH", pkt[ihl:ihl + 6])
    return (socket.inet_ntoa(pkt[12:16]), sport, socket.inet_ntoa(pkt[16:20]), dport,
            pkt[ihl + 8:ihl + max(ulen, 8)])


def parse_frame(frame, linktype=1):
    """Link-layer frame -> parse_ip() result. Ethernet, Linux cooked (SLL/SLL2), raw IP."""
    if linktype == 1:                                       # Ethernet
        off, etype = 14, struct.unpack("!H", frame[12:14])[0]
        while etype in (0x8100, 0x88A8) and len(frame) >= off + 4:   # VLAN tags
            etype = struct.unpack("!H", frame[off + 2:off + 4])[0]
            off += 4
    elif linktype == 113:                                   # tcpdump -i any (SLL)
        off, etype = 16, struct.unpack("!H", frame[14:16])[0]
    elif linktype == 276:                                   # SLL2
        off, etype = 20, struct.unpack("!H", frame[0:2])[0]
    elif linktype in (101, 228):                            # raw IPv4
        off, etype = 0, 0x0800
    else:
        return None
    return parse_ip(frame[off:]) if etype == 0x0800 else None


def info_for(msg):
    """Short description of the message body."""
    b = msg.body
    if msg.type == P.HELLO:
        return "asks %.2f Mbps, %d-byte packets, %.0f s, name %s" % (
            b["rate_mbps"], b["size"], b["duration"], b["name"])
    if msg.type == P.HELLO_ACK:
        return "accepted, reports every %d ms" % b["report_interval_ms"]
    if msg.type == P.REPORT:
        return "server got %d pkts, lost %d, %.2f Mbps, delay %.1f ms" % (
            b["received"], b["lost"], b["throughput_mbps"], b["delay_ms"])
    if msg.type == P.BYE:
        return "client sent %d packets in total" % b["total_sent"]
    if msg.type == P.SUMMARY:
        return "received %d of %d, loss %.2f %%" % (b["received"], b["total_sent"], b["loss_pct"])
    if msg.type == P.REJECT:
        return "server full" if b["reason"] == P.REJECT_SERVER_FULL else "bad request"
    if msg.type == P.ERROR:
        return "unknown session" if b["code"] == P.ERR_UNKNOWN_SESSION else "malformed datagram"
    return ""


# ------------------------------------------------------------------ display
class Decoder(object):

    HEAD = "%4s  %-15s  %-31s  %-9s  %-8s  %7s  %-15s  %9s  %s" % (
        "#", "captured", "source -> destination", "type", "session", "seq",
        "sender time", "delay", "notes")

    def __init__(self, port=None, every=1):
        self.port, self.every = port, every
        self.n = self.shown = 0
        self.flows = {}             # (src, sport, dst, dport, sid) -> stats

    def feed(self, cap_time, parsed):
        """Decode one UDP datagram; returns True if it was one of ours (and counted)."""
        if parsed is None:
            return False
        src, sport, dst, dport, payload = parsed
        if self.port and self.port not in (sport, dport):
            return False
        try:
            msg = P.unpack(payload)
        except P.ProtocolError:
            return False
        self.n += 1
        endpoints = "%s:%d -> %s:%d" % (src, sport, dst, dport)
        note, show = info_for(msg), True
        seq = "-"
        if msg.type == P.DATA:
            key = (src, sport, dst, dport, msg.session_id)
            f = self.flows.get(key)
            is_first = f is None
            if is_first:              # the capture may start in the middle of a flow
                f = self.flows[key] = {"tracker": P.SeqTracker(), "delay": [], "first": msg.seq}
                f["tracker"].max_seq = msg.seq - 1
                note = "first DATA packet of this flow in the capture"
            status, first, n = f["tracker"].update(msg.seq)
            f["delay"].append(1000 * (cap_time - msg.timestamp))
            seq = str(msg.seq)
            if status == "gap":
                note = "GAP: seq %s missing (%d lost)" % (
                    first if n == 1 else "%d-%d" % (first, first + n - 1), n)
            elif status == "late":
                note = "LATE: out of order"
            elif status == "dup":
                note = "DUPLICATE"
            show = is_first or status != "ok" or msg.seq % self.every == 0
        if show:
            self.shown += 1
            if self.shown % 25 == 1:
                print(self.HEAD)
            delay = "%.2f ms" % (1000 * (cap_time - msg.timestamp)) if msg.type == P.DATA else ""
            print("%4d  %-15s  %-31s  %-9s  %08x  %7s  %-15s  %9s  %s" % (
                self.n, P.fmt_time(cap_time), endpoints, P.TYPE_NAMES[msg.type],
                msg.session_id, seq, P.fmt_time(msg.timestamp), delay, note), flush=True)
        return True

    def summary(self):
        print("\n%d protocol datagrams decoded." % self.n)
        for (src, sport, dst, dport, sid), f in self.flows.items():
            t, d = f["tracker"], f["delay"]
            d_sorted = sorted(d)
            print("DATA %s:%d -> %s:%d (session %08x): seq %d..%d, %d received, %d missing "
                  "(%d gaps), %d out of order, %d duplicates; delay avg %.2f ms, "
                  "p95 %.2f ms, max %.2f ms" % (
                      src, sport, dst, dport, sid, f["first"], t.max_seq, t.received,
                      t.max_seq - f["first"] + 1 - t.received, t.gaps, t.reordered,
                      t.duplicates, sum(d) / len(d), d_sorted[int(0.95 * (len(d) - 1))],
                      d_sorted[-1]))


# ------------------------------------------------------------------ pcap files
class PcapWriter(object):
    def __init__(self, path):
        self.fh = open(path, "wb")
        self.fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))

    def write(self, ts, frame):
        self.fh.write(struct.pack("<IIII", int(ts), int((ts % 1) * 1e6), len(frame), len(frame)))
        self.fh.write(frame)

    def close(self):
        self.fh.close()


def read_pcap(path):
    """Yields (timestamp, linktype, frame) from a classic pcap file."""
    with open(path, "rb") as fh:
        head = fh.read(24)
        if len(head) < 24:
            sys.exit("%s: not a pcap file" % path)
        magic = head[:4]
        formats = {b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6),
                   b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9)}
        if magic == b"\x0a\x0d\x0d\x0a":
            sys.exit("%s is pcapng - in Wireshark use File > Save As > 'pcap' format, or "
                     "capture with: tcpdump -w file.pcap" % path)
        if magic not in formats:
            sys.exit("%s: not a pcap file" % path)
        endian, scale = formats[magic]
        linktype = struct.unpack(endian + "I", head[20:24])[0] & 0x0FFFFFFF
        while True:
            rec = fh.read(16)
            if len(rec) < 16:
                return
            sec, frac, incl, _ = struct.unpack(endian + "IIII", rec)
            frame = fh.read(incl)
            yield sec + frac / scale, linktype, frame


# ------------------------------------------------------------------ main
def default_iface():
    names = sorted(n for n in os.listdir("/sys/class/net") if n != "lo")
    return names[0] if names else None


def capture(args, dec):
    iface = args.iface or default_iface()
    if iface is None:
        sys.exit("no network interface found")
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    except PermissionError:
        sys.exit("raw sockets need root: run it from the Mininet CLI or with sudo")
    sock.bind((iface, 0))
    sock.settimeout(0.5)
    writer = PcapWriter(args.write) if args.write else None
    print("capturing on %s%s - %s" % (
        iface, " (UDP port %d)" % args.port if args.port else "",
        "stops after %d datagrams or %.0f s" % (args.count, args.duration)), flush=True)
    end = time.time() + args.duration
    try:
        while dec.n < args.count and time.time() < end:
            try:
                frame, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            now = time.time()
            if dec.feed(now, parse_frame(frame)) and writer:
                writer.write(now, frame)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if writer:
            writer.close()
            print("\nwrote %s (open it in Wireshark, or: python3 tools/packet_sniffer.py --read %s)"
                  % (args.write, args.write))


def main():
    ap = argparse.ArgumentParser(description="Decode our UDP protocol on the wire")
    ap.add_argument("--iface", help="interface to capture on (default: the host's first one)")
    ap.add_argument("--port", type=int, help="only datagrams to/from this UDP port")
    ap.add_argument("--count", type=int, default=20, help="stop after this many datagrams")
    ap.add_argument("--duration", type=float, default=30, help="stop after this many seconds")
    ap.add_argument("--every", type=int, default=1,
                    help="print every Nth DATA packet (gaps/late/control messages always)")
    ap.add_argument("--write", metavar="FILE.pcap", help="also save the captured packets")
    ap.add_argument("--read", metavar="FILE.pcap", help="decode a capture file instead")
    args = ap.parse_args()
    dec = Decoder(args.port, max(1, args.every))
    if args.read:
        for ts, linktype, frame in read_pcap(args.read):
            dec.feed(ts, parse_frame(frame, linktype))
    else:
        capture(args, dec)
    if dec.n:
        dec.summary()
    else:
        print("no datagrams of our protocol seen")


if __name__ == "__main__":
    main()
