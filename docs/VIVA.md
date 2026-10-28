# Viva preparation

Grouped by rubric component. **Every member should be able to answer the questions marked ★.** The viva tests individual understanding.

## Problem understanding & architecture

**★ What problem does the project solve?**
UDP senders do not slow down when the network is overloaded, because UDP has no congestion control. If several UDP flows share one path, that path's queue fills up: delay rises and packets are dropped, while the other paths stay idle. Our SDN controller detects the overloaded link and moves flows to a less-loaded path.

**★ What are the main components?**
- A Mininet topology with three parallel paths.
- Open vSwitch switches controlled with OpenFlow 1.0.
- A POX controller (`controller/congestion_controller.py`).
- A UDP client/server application (`app/`).
- A feedback channel from the server to the controller.
- Scripts to run experiments and analyse the results.

**Why three paths of equal length?**
So the only difference between paths is their *load*. Any improvement comes from choosing the least-loaded path, not from a shorter one.

**Why are the core links limited to 10 Mbps?**
To create congestion in a controlled way. Two 6 Mbps flows (about 12.4 Mbps including headers) cannot fit on one 10 Mbps path. Host links are not limited, so they are never the bottleneck.

## TCP/UDP socket implementation

**★ Which socket calls do you use?**
`socket(AF_INET, SOCK_DGRAM)`, `bind()`, `connect()` (client), `sendto()`/`send()`, and `recvfrom()`/`recv()` with timeouts. The server listens on one port and tells clients apart by `(address, session id)`.

**Why does the client `connect()` a UDP socket? UDP is connectionless.**
`connect()` on UDP sends no packets. It only fixes the default destination and makes the kernel report ICMP errors (port unreachable) as `ConnectionRefusedError`. That is how the client notices the server is not running.

**How is reliability handled for control messages?**
HELLO and BYE are retried up to 5 times, with a 1 s timeout each. The server recognises a repeated HELLO or BYE and re-sends the same reply. DATA is never retransmitted, because loss is exactly what we are measuring.

**★ How is concurrency done?**
- Server: a main thread receives and dispatches datagrams, and a reporter thread sends REPORTs every 0.5 s. A lock protects the session table they share.
- Client: a main thread sends at a fixed rate, and a receiver thread handles REPORT, ERROR and SUMMARY.
- Test TC04 shows 3 clients served by one server at the same time.

**How do you measure loss, delay and jitter?**
- Loss: gaps in the sequence numbers.
- Delay: receive time minus the timestamp in the packet. This is valid because all Mininet hosts share one clock.
- Jitter: the RFC 3550 smoothed difference of consecutive delays.

**What errors does the application handle?**
Server unreachable (exit 2), server full / REJECT (exit 3), malformed datagrams (ignored and counted), unknown session after a server restart (the client re-handshakes), send errors during a link failure (counted), and a missing feedback socket (warning, then carries on).

## Mininet + SDN implementation

**★ Which flow rules does the controller install?**

| Priority | Match | Action | Why |
|---|---|---|---|
| 300 | UDP src IP, dst IP, src port, dst port | output to the next switch on the chosen path | One rule per flow, so each flow can be moved on its own |
| 200 | ARP | to the controller | Proxy ARP: the controller answers, so nothing is flooded |
| 200 | UDP arriving from a host port (edge switches) | to the controller | The first packet of a new flow reaches the controller |
| 100 | destination IP | default path | ping and other traffic |
| 0 | everything | drop | No flooding around the loops |

**Why proxy ARP instead of flooding?**
The three paths form loops and there is no spanning tree. A flooded broadcast would circle forever (a broadcast storm). The controller knows every host's MAC from `netconfig.py`, so it answers ARP itself.

**What is a PACKET_IN, FLOW_MOD, PACKET_OUT?**
- PACKET_IN: the switch sends a packet to the controller.
- FLOW_MOD: the controller adds, changes or deletes a rule in a switch.
- PACKET_OUT: the controller tells the switch to send one specific packet.

**Which OpenFlow version and why?**
OpenFlow 1.0, because POX supports it and it has everything we need: per-flow matches, port and flow statistics, port-status messages, and flow-removed messages.

## Socket–SDN integration

**★ How does the application influence the SDN controller?**
1. Each client socket creates its own flow (its 4-tuple), and the controller installs one rule per flow, so the application's sessions are what the controller routes and moves.
2. The server sends JSON reports over a UNIX socket: session start (with the requested rate), QoS every 0.5 s (throughput, loss, delay, jitter), and session end. In `app_aware` mode, a delay of 30 ms or more (or 1% loss) on a flow whose path has a link at 80% or more triggers rerouting immediately. The requested rate also tells the controller a new flow's size before switch counters can measure it.

**How can a Mininet host reach the controller's UNIX socket?**
Mininet hosts get their own *network* namespace but share the *file system*. A UNIX socket is addressed by a file path, so every host can reach it.

**What did the integration achieve?**
In the congestion scenario, detection fell from 3.5 s (switch statistics: two polls) to 0.5 s (one application report). Overall loss fell from 0.85% to 0%, and average delay from 37–97 ms to under 2 ms (see `results/comparison.md`). Static routing lost 32%.

## Dynamic SDN functionality

**★ How is congestion detected?**
Every second the controller requests port statistics. The change in `tx_bytes` gives each link's load in Mbps. A link is congested when its load is at least 80% of capacity for 2 polls in a row, or (in app-aware mode) when the application reports bad QoS on that path. Load is taken as max(measured port rate, sum of the flow rates routed over the link), because a full link can never measure above 100%.

**★ Which flow is moved and where?**
- Excess = load − 80% of capacity. The controller moves the smallest flow that removes the whole excess on its own (newest first on ties). If none is big enough, it moves the biggest.
- The target is the least-loaded working path that keeps the flow under 80%. If no path has that much room, it takes a path only if the move still lowers the worst link load.
- This logic is in `controller/routing_logic.py` and is unit-tested.

**What is make-before-break?**
New rules are installed on the downstream switches first and the ingress switch's rule is changed last. Packets therefore never reach a switch that has no rule for them yet. The ingress rule is changed with `MODIFY_STRICT`, which keeps its byte counters.

**How do you avoid oscillation (moving flows back and forth)?**
- A 3 s cool-down on every link of the old and new path, because their counters are stale for one poll.
- A 10 s hold before the same flow can move again.
- A link counts as congested at 80% but as cleared only below 70%.

## Challenging scenario / robustness

**★ What happens when a link fails?**
The switch sends PORT_STATUS (link down). The controller marks the link as down, moves every flow on it to working paths (least-loaded first), and recomputes the default routes so ping still works. In our logs the reaction (LINK_DOWN to REROUTED) took 3 ms, and loss during the 12 s failure was 0.36%, against 96.6% for static routing. In static mode the flows stay on the failed link and lose everything until it is repaired.

**What other failures did you test?**
Server overload (REJECT), the server not running, a malformed datagram, a server restart in the middle of a stream (the client re-handshakes), and a link failure. See `tests/TEST_REPORT.md`.

## Performance evaluation

**★ What did you measure and compare?**
Throughput, packet loss, one-way delay, jitter, detection time, reroute time and recovery time. We compared static routing (baseline), adaptive (switch statistics only) and app-aware (proposed), under three conditions: congestion, link failure, and a load sweep from 62% to 145% of capacity.

**Why is recovery time longer than reroute time?**
Packets already queued on the congested link still have to drain after the reroute. That drain is also the short spike visible in the throughput graph.

**Why is static delay about 1 second?**
The Linux default queue is 1000 packets. At 10 Mbps a full queue holds about 1 s of data (bufferbloat).

## Limitations / future work

- Polling-based measurement: 1 s granularity.
- The topology is fixed in `netconfig.py`; LLDP discovery could replace it.
- One controller, so it is a single point of failure.
- A whole flow is moved; a single flow is never split across paths.
- Possible next steps: OpenFlow 1.3 meters and group tables, sFlow sampling, predictive placement of new flows.
