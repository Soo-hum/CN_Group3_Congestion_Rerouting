# Application protocol and socket design

The application is a **UDP streaming service**. Clients (`app/udp_client.py`) on h1/h2 stream constant-bit-rate UDP data to servers (`app/udp_server.py`) on h3/h4. The servers measure what arrives and report it back to the client and to the SDN controller. Everything uses plain `socket` calls: `socket()`, `bind()`, `connect()`, `sendto()`/`send()` and `recvfrom()`/`recv()`.

## 1. Message format

Every datagram starts with a 20-byte header in network byte order (`struct` format `!HBBIId`):

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 2 | magic | `0x5344` ("SD"). Anything else is rejected as malformed |
| 2 | 1 | version | `1` |
| 3 | 1 | type | message type, see below |
| 4 | 4 | session_id | random 32-bit id chosen by the client |
| 8 | 4 | seq | sequence number (DATA), used to count lost packets |
| 12 | 8 | timestamp | sender's clock (float64). One-way delay = receive time − timestamp |

| Type | Name | Direction | Body |
|---|---|---|---|
| 1 | HELLO | client → server | rate_mbps `f32`, packet size `u16`, duration `f32`, name `16s` |
| 2 | HELLO_ACK | server → client | report interval in ms `u16`, max sessions `u16` |
| 3 | REJECT | server → client | reason `u8` (1 = server full, 2 = bad request) |
| 4 | DATA | client → server | zero padding up to the chosen packet size (default 1200 bytes) |
| 5 | REPORT | server → client | received `u32`, lost `u32`, throughput `f32`, loss % `f32`, delay ms `f32`, jitter ms `f32` |
| 6 | BYE | client → server | total packets sent `u32` |
| 7 | SUMMARY | server → client | sent, received, lost `u32`, loss % `f32`, avg delay ms `f32`, avg throughput `f32` |
| 8 | ERROR | server → client | code `u8` (1 = unknown session, 2 = malformed datagram) |

Packing and parsing live in `app/protocol.py`. `tests/test_protocol.py` checks every message type and every malformed case.

## 2. Session life cycle

```
 client                                   server
   | -- HELLO (rate, size, name) ------->  |  creates a Session (or REJECT if full)
   | <------------------------ HELLO_ACK -- |
   | -- DATA seq=0,1,2,... (CBR) ------->  |  counts packets, delay, jitter, sequence gaps
   | <----------- REPORT (every 0.5 s) --  |  + JSON QoS report to the SDN controller
   | -- BYE (total sent) --------------->  |
   | <-------------------------- SUMMARY -- |  final loss = sent - received
```

UDP is unreliable, so the **control messages are retried**. HELLO is sent up to 5 times, waiting 1 s each time, and BYE up to 5 times. The server treats a repeated HELLO or BYE as a duplicate: it re-sends the same HELLO_ACK or SUMMARY instead of creating a second session.

## 3. Concurrency

| Component | Threads | What they do |
|---|---|---|
| Server | main thread | `recvfrom()` loop. Parses each datagram and dispatches it by type to the right session |
| | reporter thread | Every 0.5 s: closes the statistics interval of every session, sends REPORT, writes a CSV row, sends QoS to the controller, and expires idle sessions |
| | shared state | session table `{(client addr, session id): Session}`, protected by a `threading.Lock` |
| Client | main thread | Paces DATA packets at the requested rate. If it wakes up late it sends a short burst to catch up |
| | receiver thread | Handles HELLO_ACK, REPORT, ERROR and SUMMARY while the main thread keeps sending |

One server socket serves many clients at once. In the experiments the h4 server handles flows B and C together. Test case TC04 runs 3 clients against one server.

## 4. Error handling

| Situation | What happens |
|---|---|
| Server not running | The client uses a *connected* UDP socket, so the ICMP "port unreachable" reply surfaces as `ConnectionRefusedError`. The client retries HELLO, then exits with code **2** ("server unreachable") |
| Server full (`--max-sessions`) | The server sends REJECT. The client exits with code **3** |
| Malformed or foreign datagram | The server catches `ProtocolError`, counts it, replies ERROR(malformed) and carries on |
| DATA for an unknown session (e.g. the server was restarted) | The server replies ERROR(unknown session), at most once per second. The client sends HELLO again and carries on with the same stream (test TC12) |
| Session idle for 15 s | The server closes it and writes its summary. If packets arrive again, the session is resumed |
| Send errors (e.g. during a link failure) | Counted and reported at the end. Sending continues |
| Controller feedback socket missing | The server prints one warning and keeps working without it |
| No SUMMARY after BYE retries | The client exits with code **4** but still prints what it sent |

## 5. Feedback channel to the SDN controller (Socket–SDN integration)

The server also sends one-line JSON messages to the controller over a **UNIX datagram socket** (`/tmp/sdn_app_feedback.sock`, set in `netconfig.py`). Mininet hosts have their own network stack but share the file system with the controller, so the controller can be reached through this file path.

```json
{"type": "session_start", "name": "B", "flow": ["10.0.0.2", 40002, "10.0.0.4", 5002], "rate_mbps": 6.0, "size": 1200, "t": 1792511337.2}
{"type": "qos", "name": "A", "flow": ["10.0.0.1", 40001, "10.0.0.3", 5001], "throughput_mbps": 5.9, "loss_pct": 0.0, "delay_ms": 34.0, "jitter_ms": 0.4, "rate_mbps": 6.0, "t": 1792511337.5}
{"type": "session_end", "name": "A", "flow": [...], "reason": "bye", "t": ...}
```

`flow` is the same 4-tuple (client IP, client port, server IP, server port) that the controller uses in its per-flow OpenFlow rules. This lets the controller match an application report to the exact flow it manages. The controller uses the reports in two ways:

1. **Demand.** From `session_start` it knows a new flow's rate before switch counters can measure it. This lets it pick the right flow to move.
2. **Early warning.** A `qos` report with delay ≥ 30 ms or loss ≥ 1% while a link on that flow's path is ≥ 80% loaded counts as congestion immediately. The controller does not wait for two statistics polls.

## 6. Command-line reference

```
python3 app/udp_server.py --port 5001 [--out-dir DIR] [--interval 0.5] [--max-sessions 8]
                          [--session-timeout 15] [--duration 0] [--feedback /tmp/sdn_app_feedback.sock|none]

python3 app/udp_client.py --server 10.0.0.3 --port 5001 [--rate 6] [--size 1200] [--duration 30]
                          [--name A] [--sport 40001] [--start-at EPOCH] [--retries 5] [--timeout 1]
                          [--print-every 2] [--quiet]
```

Client exit codes: `0` ok, `2` server unreachable, `3` rejected, `4` no summary.
