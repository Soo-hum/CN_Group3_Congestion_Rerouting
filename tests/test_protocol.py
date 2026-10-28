"""Unit tests for the application protocol (app/protocol.py).

Run:  python3 -m unittest discover -s tests -v      (no Mininet / sudo needed)
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import protocol as P  # noqa: E402


class RoundTrip(unittest.TestCase):

    def test_hello(self):
        m = P.unpack(P.pack(P.HELLO, 7, body=(6.0, 1200, 30.0, "A")))
        self.assertEqual((m.type, m.session_id), (P.HELLO, 7))
        self.assertAlmostEqual(m.body["rate_mbps"], 6.0, places=4)
        self.assertEqual(m.body["size"], 1200)
        self.assertEqual(m.body["name"], "A")

    def test_long_name_is_cut_to_16_bytes(self):
        m = P.unpack(P.pack(P.HELLO, 1, body=(1.0, 100, 1.0, "x" * 40)))
        self.assertEqual(m.body["name"], "x" * 16)

    def test_data_padding_and_seq(self):
        raw = P.pack(P.DATA, 99, seq=12345, size=1200)
        self.assertEqual(len(raw), 1200)
        m = P.unpack(raw)
        self.assertEqual((m.type, m.seq, m.size), (P.DATA, 12345, 1200))

    def test_report(self):
        m = P.unpack(P.pack(P.REPORT, 5, body=(300, 12, 5.5, 3.8, 42.0, 0.7)))
        self.assertEqual((m.body["received"], m.body["lost"]), (300, 12))
        self.assertAlmostEqual(m.body["delay_ms"], 42.0, places=3)

    def test_bye_and_summary(self):
        self.assertEqual(P.unpack(P.pack(P.BYE, 5, body=(1000,))).body["total_sent"], 1000)
        s = P.unpack(P.pack(P.SUMMARY, 5, body=(1000, 990, 10, 1.0, 3.2, 5.9))).body
        self.assertEqual((s["total_sent"], s["received"], s["lost"]), (1000, 990, 10))

    def test_reject_and_error(self):
        self.assertEqual(P.unpack(P.pack(P.REJECT, 1, body=(P.REJECT_SERVER_FULL,))).body["reason"],
                         P.REJECT_SERVER_FULL)
        self.assertEqual(P.unpack(P.pack(P.ERROR, 1, body=(P.ERR_UNKNOWN_SESSION,))).body["code"],
                         P.ERR_UNKNOWN_SESSION)

    def test_timestamp_is_sent(self):
        m = P.unpack(P.pack(P.DATA, 1, timestamp=1234.5, size=64))
        self.assertEqual(m.timestamp, 1234.5)


class Malformed(unittest.TestCase):

    def test_too_short(self):
        with self.assertRaises(P.ProtocolError):
            P.unpack(b"hello")

    def test_bad_magic(self):
        raw = bytearray(P.pack(P.DATA, 1, size=100))
        raw[0] ^= 0xFF
        with self.assertRaises(P.ProtocolError):
            P.unpack(bytes(raw))

    def test_bad_version(self):
        raw = bytearray(P.pack(P.DATA, 1, size=100))
        raw[2] = 99
        with self.assertRaises(P.ProtocolError):
            P.unpack(bytes(raw))

    def test_unknown_type(self):
        raw = bytearray(P.pack(P.DATA, 1, size=100))
        raw[3] = 200
        with self.assertRaises(P.ProtocolError):
            P.unpack(bytes(raw))

    def test_truncated_body(self):
        raw = P.pack(P.REPORT, 1, body=(1, 2, 3.0, 4.0, 5.0, 6.0))
        with self.assertRaises(P.ProtocolError):
            P.unpack(raw[:-4])

    def test_hello_with_invalid_rate(self):
        with self.assertRaises(P.ProtocolError):
            P.unpack(P.pack(P.HELLO, 1, body=(0.0, 1200, 10.0, "bad")))

    def test_pack_unknown_type(self):
        with self.assertRaises(P.ProtocolError):
            P.pack(77, 1)


if __name__ == "__main__":
    unittest.main()
