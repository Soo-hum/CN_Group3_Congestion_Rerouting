"""Unit tests for the packet-level view: sequence numbers, timestamps, the
SeqTracker (gaps / out of order / duplicates) and the packet sniffer's decoder
and pcap files. Run: python3 -m unittest discover -s tests"""

import io
import os
import socket
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import protocol as P  # noqa: E402
import packet_sniffer as S  # noqa: E402
import show_paths as SP  # noqa: E402


def frame(payload, src="10.0.0.1", dst="10.0.0.3", sport=40001, dport=5001):
    """Ethernet + IPv4 + UDP frame around payload (checksums left at 0)."""
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0,
                     socket.inet_aton(src), socket.inet_aton(dst)) + udp
    return b"\x00\x00\x00\x00\x00\x03" + b"\x00\x00\x00\x00\x00\x01" + b"\x08\x00" + ip


class TestHeaderOnTheWire(unittest.TestCase):

    def test_seq_and_timestamp_positions(self):
        raw = P.pack(P.DATA, 0xABCD, seq=123456, size=1200, timestamp=1700000000.25)
        self.assertEqual(len(raw), 1200)
        self.assertEqual(struct.unpack("!I", raw[8:12])[0], 123456)          # bytes 8-11
        self.assertEqual(struct.unpack("!d", raw[12:20])[0], 1700000000.25)  # bytes 12-19
        m = P.unpack(raw)
        self.assertEqual((m.seq, m.timestamp), (123456, 1700000000.25))

    def test_fmt_time_has_microseconds(self):
        s = P.fmt_time(1700000000.123456)
        self.assertRegex(s, r"^\d\d:\d\d:\d\d\.\d{6}$")
        self.assertTrue(s.endswith(".123456") or s.endswith(".123455"))


class TestSeqTracker(unittest.TestCase):

    def test_in_order(self):
        t = P.SeqTracker()
        self.assertEqual([t.update(i)[0] for i in range(5)], ["ok"] * 5)
        self.assertEqual((t.received, t.lost, t.gaps), (5, 0, 0))

    def test_gap_then_late_packet(self):
        t = P.SeqTracker()
        for i in range(3):
            t.update(i)
        self.assertEqual(t.update(7), ("gap", 3, 4))          # 3,4,5,6 missing
        self.assertEqual(t.lost, 4)
        self.assertEqual(t.update(5)[0], "late")              # 5 was only delayed
        self.assertEqual((t.lost, t.reordered), (3, 1))

    def test_duplicate(self):
        t = P.SeqTracker()
        t.update(0)
        t.update(1)
        self.assertEqual(t.update(1)[0], "dup")
        self.assertEqual((t.received, t.duplicates), (2, 1))

    def test_first_packet_lost(self):
        t = P.SeqTracker()
        self.assertEqual(t.update(2), ("gap", 0, 2))


class TestSniffer(unittest.TestCase):

    def test_parse_frame(self):
        raw = P.pack(P.DATA, 7, seq=42, size=200)
        src, sport, dst, dport, payload = S.parse_frame(frame(raw))
        self.assertEqual((src, sport, dst, dport), ("10.0.0.1", 40001, "10.0.0.3", 5001))
        self.assertEqual(P.unpack(payload).seq, 42)

    def test_ignores_other_traffic(self):
        self.assertFalse(S.Decoder().feed(1.0, S.parse_frame(frame(b"not our protocol"))))
        self.assertIsNone(S.parse_frame(b"\x00" * 12 + b"\x08\x06" + b"\x00" * 28))   # ARP

    def test_pcap_roundtrip_finds_gap(self):
        path = os.path.join(tempfile.mkdtemp(), "t.pcap")
        w = S.PcapWriter(path)
        t = 1700000000.0
        for seq in (0, 1, 2, 5, 3):                          # 3,4 missing at first; 3 arrives late
            w.write(t + seq * 0.01, frame(P.pack(P.DATA, 9, seq=seq, size=100, timestamp=t + seq * 0.01)))
        w.close()
        dec = S.Decoder()
        out = io.StringIO()
        with redirect_stdout(out):
            for ts, lt, fr in S.read_pcap(path):
                dec.feed(ts, S.parse_frame(fr, lt))
        text = out.getvalue()
        self.assertEqual(dec.n, 5)
        self.assertIn("GAP: seq 3-4 missing (2 lost)", text)
        self.assertIn("LATE", text)
        tracker = next(iter(dec.flows.values()))["tracker"]
        self.assertEqual((tracker.lost, tracker.reordered), (1, 1))


class TestShowPaths(unittest.TestCase):
    LINE_NUM = (" cookie=0x0, duration=12.5s, table=0, n_packets=2600, n_bytes=3120000, "
                "priority=300,udp,nw_src=10.0.0.1,nw_dst=10.0.0.3,tp_src=41001,tp_dst=5001 "
                "actions=output:4")
    LINE_NAME = (" cookie=0x0, duration=3.0s, table=0, n_packets=10, n_bytes=12000, "
                 "priority=300,udp,in_port=\"s1-eth2\",nw_src=10.0.0.2,nw_dst=10.0.0.4,"
                 "tp_src=41002,tp_dst=5002 actions=output:\"s1-eth5\"")

    def test_fields(self):
        self.assertEqual(SP.field(self.LINE_NUM, "tp_src"), "41001")
        self.assertEqual(SP.field(self.LINE_NUM, "n_bytes"), "3120000")
        self.assertEqual(SP.field(self.LINE_NUM, "duration"), "12.5s")

    def test_output_port_both_formats(self):
        self.assertEqual(SP.out_port(self.LINE_NUM), 4)
        self.assertEqual(SP.out_port(self.LINE_NAME), 5)


if __name__ == "__main__":
    unittest.main()
