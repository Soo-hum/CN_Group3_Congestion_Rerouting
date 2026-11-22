# Congestion Detection and Traffic Rerouting with SDN (UDP)

**SDN–Socket Programming (Jackfruit) Mini Project · Computer Networks (UE25CS243A)**
PES University, Bengaluru · B.Tech CSE · Section H · **Group 3 · Problem 27** · Protocol: **UDP**

| # | Name | USN |
|---|------|-----|
| 1 | Samhita Sagare Venkatesh | PES1UG25CS453 |
| 2 | Sohum Marak | PES1UG26CS850 |
| 3 | Janhavi Chetankumar Bodar | PES1UG26CS821 |

> **Problem statement.** Generate UDP traffic across multiple network paths and develop an SDN controller that detects congestion and dynamically reroutes traffic through a less-loaded path.

**Stack:** Mininet · Open vSwitch · POX controller (OpenFlow 1.0) · Python 3 UDP sockets

| Want to… | Go to |
|---|---|
| **Read the full project report** | **[docs/PROJECT_REPORT.md](docs/PROJECT_REPORT.md)** · [PDF](docs/PROJECT_REPORT.pdf) |
| Install and run it step by step in Mininet | [docs/INSTALL_AND_RUN.md](docs/INSTALL_AND_RUN.md) |
| Read the application protocol | [docs/PROTOCOL.md](docs/PROTOCOL.md) |
| See the test cases and their results | [tests/TEST_REPORT.md](tests/TEST_REPORT.md) |
| See all measured results | [results/comparison.md](results/comparison.md) |

---

## 1. Problem understanding and objectives

UDP has **no congestion control**: a UDP sender keeps sending at its own rate even when the network drops its packets. When several UDP flows share one path, that path's queue fills up, so delay rises to about a second and packets are lost. Meanwhile other paths with spare capacity sit unused, because ordinary routing always picks the same shortest path.

**Objectives**
1. Build a multipath Mininet network where congestion can be created on purpose.
2. Build a UDP client/server application that generates traffic and measures throughput, loss, delay and jitter.
3. Build an SDN controller that **monitors** links and flows, **detects** congestion against a threshold, and **reroutes** flows to the least-loaded path.
4. Connect the application to the controller, so the application's own QoS measurements influence routing.
5. Handle harder situations: link failure, server overload, server restart, bad input.
6. Measure recovery time, throughput, loss and delay, and **compare against static routing** under several conditions.

**Expected network behaviour.** With static routing every flow stays on path P1. Once P1 is overloaded, loss and delay stay high until the traffic stops. With SDN rerouting, the controller notices within a few seconds (or under one second with application feedback), moves a flow to P2 or P3, and every flow gets its full rate back.

## 2. Architecture

### Topology

```
                 +------ s2 ------+          P1 = s1 -> s2 -> s5  (default path)
                 |                |
   h1 --+        |                |        +-- h3  UDP server :5001
 clients+--- s1 -+------ s3 ------+- s5 ---+     P2 = s1 -> s3 -> s5
   h2 --+        |                |        +-- h4  UDP server :5002
                 |                |              P3 = s1 -> s4 -> s5
                 +------ s4 ------+
   core links 10 Mbps (TCLink/HTB)   host links not limited   IPs 10.0.0.1-4
```

### Components

```mermaid
flowchart LR
    subgraph MN["Mininet network"]
        h1["h1: UDP client"] --- s1
        h2["h2: UDP client"] --- s1
        s1 --- s2 --- s5
        s1 --- s3 --- s5
        s1 --- s4 --- s5
        s5 --- h3["h3: UDP server 5001"]
        s5 --- h4["h4: UDP server 5002"]
    end
    POX["POX controller: congestion_controller.py"]
    POX -.->|"OpenFlow 1.0: flow rules, statistics, port status"| s1
    h3 -.->|"QoS feedback, JSON over UNIX socket"| POX
    h4 -.->|"QoS feedback"| POX
```

| Component | File | Role |
|---|---|---|
| Topology | `topology.py`, `netconfig.py` | 5 switches, 4 hosts, 3 equal paths, fixed port numbers shared with the controller |
| UDP client | `app/udp_client.py` | Handshake, constant-rate DATA stream, live reports, error handling |
| UDP server | `app/udp_server.py` | Concurrent sessions, per-interval QoS measurement, reports to clients and to the controller |
| Protocol | `app/protocol.py` | Binary message format (8 message types) |
| SDN controller | `controller/congestion_controller.py` | Flow rules, monitoring, congestion detection, rerouting, link-failure recovery |
| Decision logic | `controller/routing_logic.py` | Which flow to move and which path to use (pure functions, unit-tested) |
| Experiments | `experiment.py`, `run_all.sh` | Scenarios: congestion, link failure, load sweep, increasing number of flows, in 3 controller modes |
| Evaluation | `analyze.py` | Metrics, comparison tables, graphs |
| Packet tools | `tools/` | Raw-socket sniffer that decodes seq + timestamps on the wire, per-packet trace summary, which path each flow uses |
| Tests | `tests/` | 39 unit tests + 14 automated Mininet test cases |

### Communication flow

```mermaid
sequenceDiagram
    participant C as Client h1
    participant S1 as Switch s1
    participant P as POX controller
    participant S as Server h3
    C->>S1: HELLO (first packet of a new flow)
    S1->>P: PACKET_IN
    P->>S1: FLOW_MOD per-flow rule on path P1 (installed s5, s2, then s1)
    P->>S1: PACKET_OUT (forward the HELLO)
    S1->>S: HELLO
    S->>C: HELLO_ACK
    S-->>P: session_start (name, requested rate)
    loop every 0.5 s
        C->>S: DATA (seq, timestamp)
        S->>C: REPORT (throughput, loss, delay, jitter)
        S-->>P: qos report
    end
    loop every 1 s
        P->>S1: STATS_REQUEST (ports and flows)
        S1->>P: STATS_REPLY
    end
    Note over P: link at 80 percent or more, or app reports high delay
    P->>S1: FLOW_MOD modify: move a flow to P2 or P3
    C->>S: BYE (packets sent)
    S->>C: SUMMARY (final loss, delay, throughput)
```

## 3. UDP socket implementation

A small **application protocol** on top of UDP (full spec: [docs/PROTOCOL.md](docs/PROTOCOL.md)):

- **Message types:** HELLO, HELLO_ACK, REJECT, DATA, REPORT, BYE, SUMMARY, ERROR. All share a 20-byte binary header (magic, version, type, session id, sequence number, timestamp).
- **Client/server:** the client opens a session with HELLO, streams DATA at a constant rate, gets a REPORT every 0.5 s, and closes with BYE/SUMMARY. HELLO and BYE are retried, because UDP may lose them.
- **Concurrency:** one server socket serves many clients. The server's receive thread and reporter thread share a lock-protected session table. The client sends in one thread and receives reports in another. In the experiments, the h4 server handles flows B and C at the same time.
- **Error handling:** server unreachable (ICMP port unreachable through a connected UDP socket), server full (REJECT), malformed datagrams, unknown session after a server restart (the client re-handshakes), send failures during link outages, idle-session timeout, and the controller channel being unavailable.
- **Measurement:** loss from sequence-number gaps, one-way delay from timestamps (Mininet hosts share one clock), and RFC 3550 jitter. Each session is written to `flow_<name>.csv`.
- **Sequence numbers and timestamps, packet by packet:** every DATA packet carries a 32-bit sequence number (bytes 8–11) and the sender's timestamp (bytes 12–19). The receiver classifies each packet as in order, **gap** (packets before it are missing = lost), **late** (out of order) or **duplicate**.
  - `udp_client.py --show-packets [N]` prints the seq and timestamp of every Nth packet it sends.
  - `udp_server.py --show-packets [N]` prints seq, send time, receive time and one-way delay of every Nth packet, and every gap or late packet.
  - `udp_server.py --trace` writes `trace_<flow>.csv` (one row per packet); `tools/trace_summary.py` lists the gaps with their times.
  - `tools/packet_sniffer.py` captures with a **raw socket (AF_PACKET)** on a Mininet host and decodes our header on the wire; it can save a `.pcap` for Wireshark.

## 4. Mininet + SDN implementation

Rules the controller installs:

| Priority | Match | Action | Purpose |
|---|---|---|---|
| 300 | UDP: src IP, dst IP, src port, dst port | output to the next switch on the chosen path | One rule per flow, so each flow can be rerouted on its own |
| 200 | ARP | controller | Proxy ARP: the controller answers, so no broadcast loops around the 3 paths |
| 200 | UDP from a host port (edge switches) | controller | The first packet of a new flow reaches the controller |
| 100 | destination IP | default (shortest working) path | ping and other traffic |
| 0 | everything else | drop | No flooding in a looped topology |

Controller **modes** (`--mode`):

| Mode | Behaviour |
|---|---|
| `static` | **Baseline.** Flows stay on P1. Congestion and link failures are only logged |
| `adaptive` | Rerouting driven by OpenFlow port and flow statistics, plus link-failure recovery |
| `app_aware` | **Proposed.** `adaptive` + QoS feedback and flow demand from the UDP application |

## 5. Socket–SDN integration

The application and the network affect each other in both directions:

1. **Application → flow rules.** Every client socket is its own flow (its 4-tuple). The first packet of a session (HELLO) triggers PACKET_IN, and the controller installs a rule for exactly that flow. A session is the unit the controller monitors and moves.
2. **Application → controller decisions.** The UDP server sends JSON messages over a UNIX datagram socket (`/tmp/sdn_app_feedback.sock`): `session_start` (with the requested rate), a `qos` report every 0.5 s, and `session_end`. In `app_aware` mode:
   - a report with **delay ≥ 30 ms or loss ≥ 1%** on a flow whose path has a link at 80% or more triggers rerouting immediately, without waiting for two statistics polls;
   - the requested rate tells the controller how big a new flow is before switch counters can measure it.
3. **Network → application.** The application sees the effect directly: its delay and loss drop right after a reroute. The server's REPORTs show this to each client live.

The controller writes `APP_SESSION_START`, `APP_QOS_ALERT` and `APP_QOS_OK` events and `app_qos.csv`, so the integration is visible in the logs (test case TC11).

## 6. Dynamic SDN functionality

```mermaid
flowchart TD
    A["Every 1 s: port and flow statistics"] --> B{"Link load at least 80 percent for 2 polls?"}
    Q["App QoS report: delay at least 30 ms or loss at least 1 percent"] --> R{"Link on that flow's path at least 80 percent?"}
    B -->|no| A
    B -->|yes| D{"Controller mode"}
    R -->|yes| D
    D -->|static| L["Log only"]
    D -->|adaptive or app_aware| E["Pick flow: smallest one that removes the excess (repeat while still over 80 percent)"]
    E --> F["Pick path: least-loaded working path with room"]
    F --> G["Install rules egress first, modify the ingress rule"]
    G --> H["3 s cool-down on affected links"]
    X["PORT_STATUS: link down"] --> Y["Move every flow on that link and recompute default routes"]
```

- **Monitoring:** port statistics give each link's load from the change in `tx_bytes`. Flow statistics give each flow's rate at the ingress switch. Load = max(measured port rate, sum of the flow rates routed over the link).
- **Detection:** threshold 80% of capacity for 2 consecutive polls (port statistics), or one QoS alert from the application (`app_aware`).
- **Rerouting:** move the smallest flow that removes the excess, to the least-loaded working path that keeps it under 80%. If the link is still over 80% (many flows arrived at once), the next flow is moved in the same decision, each to the best path at that moment. Rules are installed **make-before-break**, and `MODIFY_STRICT` keeps the counters.
- **Load balancing of many flows:** every new flow starts on P1; as flows keep arriving the controller spreads them over P1/P2/P3 (12 flows of 2 Mbps end up 4/4/4). When every path is near the threshold it leaves the flows where they are (`NO_ALTERNATE_PATH`, logged once per congestion episode).
- **Stability:** cool-down on the old and new path's links, a 10 s hold per flow, and clearing only below 70%.
- **Recovery:** on a link-down PORT_STATUS, every affected flow is moved at once and the default routes are recomputed.

## 7. Challenging scenarios / robustness

| Scenario | How it is created | What the system does | Shown in |
|---|---|---|---|
| **Congestion** | 2–3 flows of 6 Mbps on a 10 Mbps path | Detects it and moves a flow to P2, then the next to P3 | `experiment.py --scenario congestion`, TC08 |
| **Link failure** | `link s1 s2 down` / `configLinkStatus` | Moves flows within milliseconds; ping keeps working; the per-packet trace shows exactly which sequence numbers were lost | `--scenario linkfail`, TC10 |
| **Many flows** | 1 to 12 flows of 2 Mbps joining every 2 s (up to 24 Mbps; one path has 10) | Spreads the flows over the three paths; 0% loss | `--scenario flows`, TC14 |
| **Server overload** | `--max-sessions 2`, 3 clients | Third client gets REJECT and exits cleanly | TC05 |
| **Service failure** | Server killed and restarted mid-stream | Client detects the unknown session, re-handshakes and continues | TC12 |
| **Bad input** | Non-protocol datagram, no server listening | Ignored and counted; client reports "unreachable" | TC06 |

## 8. Performance evaluation and comparison

**Metrics:** throughput, packet loss, one-way delay, jitter, detection time, reroute time, recovery time, fairness (Jain's index), and per packet: sequence-number gaps, longest time without packets, out-of-order packets.
**Baseline:** static routing. **Conditions:** (1) congestion with 3 flows, (2) link failure (also analysed packet by packet), (3) load sweep with offered load on P1 from 62% to 145% of capacity, (4) increasing number of flows, 1 to 12.

**Key findings (our run)**
- **Congestion:** static routing lost **30%** of packets, with about 1 s of queueing delay (the third flow got only 0.24 of its 6 Mbps). Adaptive rerouting cut loss to **0.73%** (detection after 2.6 s). App-aware rerouting reached **0% loss** and 2–4 ms average delay, because the application's delay report triggered the reroute after **0.7 s**.
- **Link failure:** static lost **96%** of packets for the 12 s outage. The controller moved both flows **3 ms** after the PORT_STATUS message, with **0.36%** loss.
- **Link failure, packet by packet:** with static routing each flow's sequence numbers stop for 12.0 s and jump by 3742 (one gap of 3742 lost packets). With the controller each flow misses only 14 packets (one gap, 48 ms without packets).
- **Load sweep:** static routing breaks down above 100% load (445 ms delay at 103%, then 17–29% loss). SDN rerouting keeps at most **0.06% loss** and under 2 ms delay at every load up to 145%.
- **Increasing number of flows:** static routing delivers at most 9.7 Mbps whatever the number of flows; loss grows from 12% (6 flows) to **48%** (12 flows). The controller spreads the flows over the three paths (12 flows: 4/4/4) and delivers all 24 Mbps with at most 0.04% loss and fairness 1.0.

*Environment: Mininet 2.3, Open vSwitch 3.3 (userspace datapath), POX 0.7.0, Python 3.13, 2-CPU VM. Re-running `sudo ./run_all.sh` on your VM gives slightly different numbers but the same picture.*

<!--RESULTS-->
### 8.1. Congestion (3 flows of 6 Mbps, 10 Mbps links)

- Flow A: h1 -> h3, 6 Mbps, t = 2-52 s
- Flow B: h2 -> h4, 6 Mbps, t = 12-52 s
- Flow C: h1 -> h4, 6 Mbps, t = 27-47 s

| Flow | Mode | Avg throughput (Mbps) | Loss | Avg delay (ms) | Max delay (ms) | Jitter (ms) |
|---|---|---|---|---|---|---|
| A | Static (baseline) | 5.20 of 6 | 11.55 % | 723.8 | 1006.5 | 0.35 |
| A | Adaptive (switch stats) | 6.00 of 6 | 0.00 % | 54.2 | 580.2 | 0.17 |
| A | App-aware (proposed) | 6.00 of 6 | 0.00 % | 2.9 | 73.4 | 0.14 |
| B | Static (baseline) | 4.52 of 6 | 22.86 % | 935.7 | 1006.5 | 0.38 |
| B | Adaptive (switch stats) | 6.01 of 6 | 0.99 % | 20.8 | 385.0 | 10.42 |
| B | App-aware (proposed) | 6.00 of 6 | 0.00 % | 1.6 | 70.7 | 0.12 |
| C | Static (baseline) | 0.24 of 6 | 95.67 % | 995.3 | 998.2 | 0.29 |
| C | Adaptive (switch stats) | 6.01 of 6 | 2.04 % | 41.9 | 383.5 | 26.60 |
| C | App-aware (proposed) | 6.01 of 6 | 0.00 % | 4.3 | 73.5 | 0.16 |

| Mode | Packets delivered | Packets lost | Overall loss |
|---|---|---|---|
| Static (baseline) | 45893 | 19639 | 29.97 % |
| Adaptive (switch stats) | 67862 | 501 | 0.73 % |
| App-aware (proposed) | 67839 | 0 | 0.00 % |

| Event | Mode | Detected after | Detected by | Rerouted after | Recovered within | Action |
|---|---|---|---|---|---|---|
| flow B joins (t=12s) | Static (baseline) | 3.3 s | port-stats | - | not within 15 s | none |
| flow B joins (t=12s) | Adaptive (switch stats) | 2.6 s | port-stats | 2.6 s | 3.8 s | flow B P1 -> P2 |
| flow B joins (t=12s) | App-aware (proposed) | 0.7 s | application | 0.7 s | 1.2 s | flow B P1 -> P2 |
| flow C joins (t=27s) | Static (baseline) | already congested | - | - | not within 25 s | none |
| flow C joins (t=27s) | Adaptive (switch stats) | 2.6 s | port-stats | 2.6 s | 3.8 s | flow C P1 -> P3 |
| flow C joins (t=27s) | App-aware (proposed) | 0.7 s | application | 0.7 s | 1.2 s | flow A P1 -> P3 |

![Per-flow throughput in the congestion scenario](results/congestion_throughput.png)

![Load on each path](results/congestion_paths.png)

![Loss and delay per flow](results/congestion_summary.png)

### 8.2. Link failure (link s1-s2 of path P1 fails at t=12s, repaired at t=24s)

Two 3 Mbps flows (A: h1->h3, B: h2->h4) run on P1 when the link fails.

| Mode | Loss during failure | Overall loss | Reaction (LINK_DOWN -> REROUTED) | Recovered within | Action |
|---|---|---|---|---|---|
| Static (baseline) | 95.80 % | 31.93 % | - | only when the link is repaired | none (flows stay on the failed path) |
| App-aware (proposed) | 0.36 % | 0.12 % | 3 ms | 0.7 s | A -> P3, B -> P2 |

![Link failure](results/linkfail_throughput.png)

### 8.3. Packet-level view of the link failure (sequence numbers and timestamps)

Every DATA packet carries a sequence number and the sender's timestamp. The server writes one row per packet (`trace_<flow>.csv`); a jump in the sequence numbers is a gap = lost packets. Silence = longest time without any packet of the flow. Inspect a trace with `python3 tools/trace_summary.py results/linkfail/<mode>/trace_A.csv`.

| Mode | Flow | Packets received | Gaps | Packets lost | Largest gap (packets) | Longest silence | Out of order | Delay p50 / p95 / max (ms) |
|---|---|---|---|---|---|---|---|---|
| Static (baseline) | A | 8133 | 1 | 3742 | 3742 | 12.0 s | 0 | 0.49 / 0.75 / 4.7 |
| Static (baseline) | B | 8133 | 1 | 3742 | 3742 | 12.0 s | 0 | 0.43 / 0.66 / 4.4 |
| App-aware (proposed) | A | 11861 | 1 | 14 | 14 | 48 ms | 0 | 0.56 / 0.89 / 6.4 |
| App-aware (proposed) | B | 11861 | 1 | 14 | 14 | 48 ms | 0 | 0.40 / 0.65 / 6.7 |

With static routing the sequence numbers stop for the whole failure (t=12 s) and jump when the link is repaired; with the SDN controller only a few packets around t=12 s are missing.

![Link failure packet by packet: sequence numbers and timestamps](results/linkfail_packets.png)

### 8.4. Load sweep (2 flows on P1; offered load = both flows together)

| Per-flow rate | Offered load on P1 | Mode | Loss | Avg delay (ms) | Avg throughput per flow (Mbps) | Reroutes |
|---|---|---|---|---|---|---|
| 3 Mbps | 62 % | Static (baseline) | 0.00 % | 0.5 | 3.00 | 0 |
| 3 Mbps | 62 % | App-aware (proposed) | 0.00 % | 0.6 | 3.00 | 0 |
| 4 Mbps | 83 % | Static (baseline) | 0.00 % | 0.5 | 4.00 | 0 |
| 4 Mbps | 83 % | App-aware (proposed) | 0.00 % | 0.5 | 4.00 | 1 |
| 5 Mbps | 103 % | Static (baseline) | 0.00 % | 445.4 | 4.82 | 0 |
| 5 Mbps | 103 % | App-aware (proposed) | 0.06 % | 1.9 | 5.00 | 1 |
| 6 Mbps | 124 % | Static (baseline) | 16.58 % | 914.2 | 4.83 | 0 |
| 6 Mbps | 124 % | App-aware (proposed) | 0.00 % | 0.5 | 6.00 | 1 |
| 7 Mbps | 145 % | Static (baseline) | 28.62 % | 950.0 | 4.83 | 0 |
| 7 Mbps | 145 % | App-aware (proposed) | 0.00 % | 0.5 | 7.00 | 1 |

![Load sweep](results/sweep.png)

### 8.5. Increasing number of flows (2 Mbps each; P1 = 10 Mbps, 3 paths = 30 Mbps)

N flows join one by one (every 2 s) on the default path P1 and all run until 20 s after the last one joined. Throughput and fairness are measured while all flows run (from 6 s after the last flow joined); loss and delay over the whole run. Fairness = Jain's index of throughput / requested rate (1 = every flow gets what it asked for).

| Flows | Offered (Mbps) | Mode | Delivered (Mbps) | Loss | Avg delay (ms) | Max delay (ms) | Fairness | Reroutes | Flows on P1 / P2 / P3 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 2 | Static (baseline) | 2.00 | 0.00 % | 0.5 | 1 | 1.000 | 0 | 1 / 0 / 0 |
| 1 | 2 | App-aware (proposed) | 2.00 | 0.00 % | 0.5 | 1 | 1.000 | 0 | 1 / 0 / 0 |
| 2 | 4 | Static (baseline) | 4.00 | 0.00 % | 0.5 | 1 | 1.000 | 0 | 2 / 0 / 0 |
| 2 | 4 | App-aware (proposed) | 4.00 | 0.00 % | 0.5 | 1 | 1.000 | 0 | 2 / 0 / 0 |
| 4 | 8 | Static (baseline) | 8.00 | 0.00 % | 0.5 | 1 | 1.000 | 0 | 4 / 0 / 0 |
| 4 | 8 | App-aware (proposed) | 8.00 | 0.00 % | 0.5 | 1 | 1.000 | 1 | 3 / 1 / 0 |
| 6 | 12 | Static (baseline) | 9.66 | 12.29 % | 680.5 | 1003 | 0.859 | 0 | 6 / 0 / 0 |
| 6 | 12 | App-aware (proposed) | 12.00 | 0.00 % | 0.6 | 10 | 1.000 | 3 | 3 / 2 / 1 |
| 8 | 16 | Static (baseline) | 9.66 | 29.03 % | 735.4 | 1001 | 0.800 | 0 | 8 / 0 / 0 |
| 8 | 16 | App-aware (proposed) | 16.00 | 0.03 % | 1.8 | 37 | 1.000 | 5 | 3 / 3 / 2 |
| 10 | 20 | Static (baseline) | 9.66 | 35.23 % | 750.4 | 999 | 0.849 | 0 | 10 / 0 / 0 |
| 10 | 20 | App-aware (proposed) | 20.00 | 0.00 % | 2.0 | 35 | 1.000 | 10 | 3 / 4 / 3 |
| 12 | 24 | Static (baseline) | 9.66 | 47.84 % | 795.8 | 1016 | 0.773 | 0 | 12 / 0 / 0 |
| 12 | 24 | App-aware (proposed) | 24.00 | 0.04 % | 2.3 | 39 | 1.000 | 8 | 4 / 4 / 4 |

![Increasing number of flows](results/flows.png)

<!--/RESULTS-->

## 9. Test cases

`python3 -m unittest discover -s tests` runs **39 unit tests** (protocol encoding and malformed input, sequence-number tracking, the packet sniffer's decoder and pcap files, flow and path selection, topology consistency). They need no Mininet.

`sudo python3 tests/run_tests.py` runs **14 functional test cases** in Mininet, each with a fresh network and controller:

<!--TESTS-->
**14 of 14 test cases passed.**

| ID | Test case | Rubric area | Controller mode | Result | Observed |
|---|---|---|---|---|---|
| TC01 | Topology and connectivity (pingall) | Mininet + SDN | app_aware | PASS | pingall loss 0%, 5 switches, 4 hosts, 3 paths s1->s5 |
| TC02 | Controller installs base flow rules | Mininet + SDN | app_aware | PASS | s1: 4 default routes, ARP->ctrl 1, UDP->ctrl 2, drop yes; s3: 4 default routes, ARP->ctrl 1, UDP->ctrl 0, drop yes; s5: 4 default routes, ARP->ctrl 1, UDP->ctrl 2, drop yes |
| TC03 | UDP client-server session (HELLO/DATA/BYE) | Socket implementation | app_aware | PASS | client exit 0, handshake ok, SUMMARY loss 0.0%, server CSV written |
| TC04 | Concurrent clients on one server | Socket implementation | app_aware | PASS | 3 clients -> one server: exit codes [0, 0, 0], losses [0.0, 0.0, 0.0] %, server had 3 sessions at once: True |
| TC05 | Server overload: extra client rejected | Robustness | app_aware | PASS | max 2 sessions: clients 1-2 exit 0/0, client 3 exit 3 (REJECTED) |
| TC06 | Error handling: no server / malformed packet | Socket implementation | app_aware | PASS | no server -> client exit 2 (unreachable detected); malformed datagram ignored by server; next client exit 0 |
| TC07 | Per-flow rule installed on default path P1 | Socket-SDN integration | app_aware | PASS | per-flow rule on s1: found, output port 3 (P1 = port 3) |
| TC08 | Congestion detected and flow rerouted | Dynamic SDN | app_aware | PASS | detected True, rerouted h2:40002->h4:5002(B) after 0.5s (P2 (via s3)), loss A 0.0% B 0.0%, flows use ports [3, 4] |
| TC09 | Static baseline: congestion, no rerouting | Performance baseline | static | PASS | static: detected True, rerouted False, loss A 16.01% B 1.79% (expected clear loss because nothing is rerouted) |
| TC10 | Link failure: flows moved, ping still works | Robustness | app_aware | PASS | LINK_DOWN True, flows moved 2 (P2 (via s3)), client loss 0.4%, ping during failure 0% loss |
| TC11 | Application QoS feedback reaches controller | Socket-SDN integration | app_aware | PASS | controller got session start True, 8 QoS reports, session end True, flow labelled with app name True |
| TC12 | Service failure: server restart, client recovers | Robustness | app_aware | PASS | server killed for 2 s: client exit 0, re-handshake True, new server took the session True |
| TC13 | Packet trace: sequence numbers and timestamps | Socket implementation | app_aware | PASS | client printed seq+timestamp True, server printed seq+send/recv time+delay True, trace: 313 of 313 packets, seq 0..312 in order True, delay 0.30-3.30 ms, sniffer decoded HELLO + DATA seq 0 on the wire True |
| TC14 | 8 flows spread over the three paths | Dynamic SDN (load balancing) | app_aware | PASS | 8 flows of 2 Mbps (16 Mbps, P1 alone has 10): flows per path {'P1': 3, 'P2': 3, 'P3': 2}, losses [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] % |
<!--/TESTS-->

## 10. How to run

Full step-by-step guide, matching the course's Mininet installation manual: **[docs/INSTALL_AND_RUN.md](docs/INSTALL_AND_RUN.md)**.

```bash
# one-time setup (Ubuntu VM)
sudo apt update && sudo apt install -y mininet openvswitch-switch git python3-matplotlib
git clone https://github.com/noxrepo/pox.git ~/pox

# live demo: two terminals
./run_controller.sh app_aware                # terminal 1
sudo python3 topology.py                     # terminal 2  -> mininet> CLI (one command at a time)

# reproduce all results and graphs
sudo ./run_all.sh                            # about 25 min (--quick: about 4 min, --only flows: one scenario)
sudo python3 tests/run_tests.py              # about 5 min
```

## 11. Rubric mapping

| Deliverable | Component (marks) | Where it is addressed |
|---|---|---|
| D1 | Problem Understanding & Architecture (2) | §1, §2: objectives, topology, component and sequence diagrams |
| D1 | TCP/UDP Socket Implementation (4) | §3, `app/`, [PROTOCOL.md](docs/PROTOCOL.md); TC03, TC04, TC06; 14 protocol unit tests |
| D1 | Mininet + SDN Implementation (4) | §4, `topology.py`, `controller/`; TC01, TC02, TC07 |
| D1 | Socket–SDN Integration (3) | §5: per-session flow rules, application QoS feedback channel; TC07, TC11 |
| D1 | Initial Testing & Demo (2) | §9, [TEST_REPORT.md](tests/TEST_REPORT.md) |
| D2 | Complete Application Functionality (4) | Up to 12 concurrent flows on 2 servers, full session life cycle, sequence numbers and timestamps per packet (client, server, trace, on the wire); TC03–TC06, TC13, TC14 |
| D2 | Dynamic SDN Functionality (6) | §6: monitoring, threshold detection, rerouting, load balancing of many flows, link-failure recovery; TC08, TC10, TC14 |
| D2 | Challenging Scenario / Robustness (4) | §7: congestion, link failure, many flows, server overload, service failure, bad input |
| D2 | Performance Evaluation & Comparison (5) | §8: 10 metrics, 4 conditions (congestion, link failure, load sweep, increasing flows), static baseline vs 2 SDN modes, [comparison.md](results/comparison.md) |
| D2 | Documentation, Final Demo & Viva (6) | [Project report](docs/PROJECT_REPORT.md), this README, `docs/`, reproducible scripts |

## 12. Repository structure

```
CN_Group3_Congestion_Rerouting/
├── README.md                    this file
├── netconfig.py                 topology description shared by Mininet and the controller
├── topology.py                  Mininet topology + interactive CLI
├── app/
│   ├── protocol.py              application protocol (message formats)
│   ├── udp_server.py            concurrent UDP server (+ feedback to the controller)
│   └── udp_client.py            UDP streaming client
├── controller/
│   ├── congestion_controller.py POX controller
│   └── routing_logic.py         flow/path selection (unit-tested)
├── experiment.py                one experiment (starts its own controller)
├── run_all.sh                   all experiments + analysis
├── run_controller.sh            controller for the live demo
├── analyze.py                   metrics, tables, graphs
├── tools/
│   ├── packet_sniffer.py        raw-socket capture: decodes seq + timestamps on the wire, .pcap
│   ├── trace_summary.py         per-packet trace: gaps (lost packets) with their times
│   └── show_paths.py            which path every UDP flow uses right now (reads s1's rules)
├── demo/
│   └── flows8.mn                Mininet CLI script: 2 servers + 8 flows (mininet> source demo/flows8.mn)
├── tests/
│   ├── test_protocol.py         unit tests
│   ├── test_packets.py          unit tests (seq tracking, sniffer, pcap)
│   ├── test_routing_logic.py    unit tests
│   ├── run_tests.py             14 Mininet test cases
│   └── TEST_REPORT.md           latest test results
├── results/                     measured results (comparison.md, *.png, raw logs)
└── docs/                        PROJECT_REPORT (.md + .pdf), INSTALL_AND_RUN, PROTOCOL
```

### Publishing to GitHub

```bash
cd CN_Group3_Congestion_Rerouting
git init
git add .
git commit -m "SDN congestion detection and UDP traffic rerouting - Group 3"
git branch -M main
git remote add origin https://github.com/<your-username>/CN_Group3_Congestion_Rerouting.git
git push -u origin main
```
Create the empty repository on github.com first, and add your teammates as collaborators (Settings → Collaborators).

## 13. Limitations and future work

- Statistics are polled every 1 s, so detection from switch counters alone takes 2–3 s. The application feedback reduces this.
- The topology is described in `netconfig.py`; LLDP-based discovery (`openflow.discovery`) would make it generic.
- A whole flow is moved; one flow is never split over several paths. There is a single controller.
- New flows always start on P1 and are moved afterwards. With many flows all paths eventually sit near the threshold; the controller then keeps flows where they are, so admission control would be the next step.
- Future work: OpenFlow 1.3 meters and group tables (fast failover), sFlow sampling, placing new flows on the least-loaded path at admission.

## References

1. Mininet: <https://mininet.org/overview/>, <https://mininet.org/walkthrough/>
2. POX controller: <https://github.com/noxrepo/pox>
3. OpenFlow Switch Specification 1.0.0 (Open Networking Foundation)
4. RFC 768 (UDP), RFC 3550 (RTP – interarrival jitter)
