"""
protocol.py - Application-level protocol used by udp_client.py and udp_server.py.

Every datagram starts with the same 20-byte header (network byte order):

    0       2    3    4            8            12                  20
    +-------+----+----+------------+------------+-------------------+
    | magic |ver |type| session_id |    seq     |  timestamp (f64)  |
    +-------+----+----+------------+------------+-------------------+

followed by a type-specific body:

    type         dir    body
    HELLO        C->S   rate_mbps f32, size u16, duration f32, name 16s
    HELLO_ACK    S->C   report_interval_ms u16, max_sessions u16
    REJECT       S->C   reason u8          (1 = server full, 2 = bad request)
    DATA         C->S   padding up to the packet size chosen by the client
    REPORT       S->C   received u32, lost u32, throughput f32, loss_pct f32,
                        delay_ms f32, jitter_ms f32        (per report interval)
    BYE          C->S   total_sent u32
    SUMMARY      S->C   total_sent u32, received u32, lost u32, loss_pct f32,
                        avg_delay_ms f32, avg_throughput f32
    ERROR        S->C   code u8            (1 = unknown session, 2 = malformed)

The timestamp is the sender's clock; inside Mininet all hosts share one
clock, so receive_time - timestamp is the real one-way delay.
"""

import struct
import time
from collections import namedtuple

MAGIC = 0x5344          # "SD"
VERSION = 1

HELLO, HELLO_ACK, REJECT, DATA, REPORT, BYE, SUMMARY, ERROR = range(1, 9)
TYPE_NAMES = {HELLO: "HELLO", HELLO_ACK: "HELLO_ACK", REJECT: "REJECT", DATA: "DATA",
              REPORT: "REPORT", BYE: "BYE", SUMMARY: "SUMMARY", ERROR: "ERROR"}

REJECT_SERVER_FULL = 1
REJECT_BAD_REQUEST = 2
ERR_UNKNOWN_SESSION = 1
ERR_MALFORMED = 2

HEADER = struct.Struct("!HBBIId")
BODIES = {
    HELLO: struct.Struct("!fHf16s"),
    HELLO_ACK: struct.Struct("!HH"),
    REJECT: struct.Struct("!B"),
    DATA: struct.Struct(""),
    REPORT: struct.Struct("!IIffff"),
    BYE: struct.Struct("!I"),
    SUMMARY: struct.Struct("!IIIfff"),
    ERROR: struct.Struct("!B"),
}
FIELDS = {
    HELLO: ("rate_mbps", "size", "duration", "name"),
    HELLO_ACK: ("report_interval_ms", "max_sessions"),
    REJECT: ("reason",),
    DATA: (),
    REPORT: ("received", "lost", "throughput_mbps", "loss_pct", "delay_ms", "jitter_ms"),
    BYE: ("total_sent",),
    SUMMARY: ("total_sent", "received", "lost", "loss_pct", "avg_delay_ms",
              "avg_throughput_mbps"),
    ERROR: ("code",),
}
MIN_DATA_SIZE = HEADER.size

Message = namedtuple("Message", "type session_id seq timestamp body size")


class ProtocolError(ValueError):
    """Raised for datagrams that do not follow the protocol."""


def pack(msg_type, session_id, seq=0, body=(), size=0, timestamp=None):
    """Build one datagram. `body` is a tuple in the order listed in FIELDS.
    For DATA, `size` is the total datagram size (header + padding)."""
    if msg_type not in BODIES:
        raise ProtocolError("unknown message type %r" % msg_type)
    if msg_type == HELLO:
        rate, psize, duration, name = body
        name = name.encode() if isinstance(name, str) else name
        body = (rate, psize, duration, name[:16])
    ts = time.time() if timestamp is None else timestamp
    data = HEADER.pack(MAGIC, VERSION, msg_type, session_id & 0xFFFFFFFF,
                       seq & 0xFFFFFFFF, ts) + BODIES[msg_type].pack(*body)
    if size > len(data):
        data += b"\x00" * (size - len(data))
    return data


def unpack(data):
    """Parse one datagram into a Message; body is a dict. Raises ProtocolError."""
    if len(data) < HEADER.size:
        raise ProtocolError("datagram too short (%d bytes)" % len(data))
    magic, version, msg_type, sid, seq, ts = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ProtocolError("bad magic 0x%04x" % magic)
    if version != VERSION:
        raise ProtocolError("unsupported version %d" % version)
    if msg_type not in BODIES:
        raise ProtocolError("unknown message type %d" % msg_type)
    body_struct = BODIES[msg_type]
    if len(data) < HEADER.size + body_struct.size:
        raise ProtocolError("%s body truncated" % TYPE_NAMES[msg_type])
    values = body_struct.unpack_from(data, HEADER.size)
    body = dict(zip(FIELDS[msg_type], values))
    if msg_type == HELLO:
        body["name"] = body["name"].rstrip(b"\x00").decode("ascii", "replace")
        if not (0 < body["rate_mbps"] < 10000) or body["size"] < MIN_DATA_SIZE:
            raise ProtocolError("HELLO with invalid rate/size")
    return Message(msg_type, sid, seq, ts, body, len(data))
