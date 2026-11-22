# Demo 2 script (D2 – final demo, 25 marks, about 15 minutes)

Demo 1 showed that the system works. Demo 2 shows that it works **completely, dynamically, under stress, and measurably better than static routing**. Everything below was run exactly as written; the example outputs are real (your times, session ids and exact numbers will differ).

## What D2 marks, and where you show it

| D2 component (marks) | What you show | Part |
|---|---|---|
| Complete Application Functionality (4) | Sequence number + timestamp of every packet (client, server, on the wire), many concurrent flows on two servers | 2, 3 |
| Dynamic SDN Functionality (6) | Controller spreads 8 flows over 3 paths as they arrive; monitoring, rerouting, load balancing | 3 |
| Challenging Scenario / Robustness (4) | Link failure while 8 flows run: flows moved in milliseconds, the per-packet trace shows exactly which packets were lost | 4 |
| Performance Evaluation & Comparison (5) | Static vs. SDN under 4 conditions: congestion, link failure (also packet by packet), load sweep, increasing number of flows | 5 |
| Documentation, Final Demo & Viva (6) | README, test report (14 test cases), GitHub, viva answers ([VIVA.md](VIVA.md)) | 6 |

Suggested split: one member per two parts, but **everyone should be able to run any step and answer any question**; the viva marks individual understanding.

## Before the demo (15 minutes early)

**Give the VM at least 4 CPUs** (VirtualBox: shut the VM down → Settings → System → Processor → 4). With 2 CPUs the 8 flows of Part 3 overload the VM: commands react slowly and the application reports 100–500 ms delays that are caused by the busy CPU, not by the network.

```bash
cd ~/CN_Group3_Congestion_Rerouting
sudo mn -c                                   # clean up anything left over
sudo pkill -f pox.py                         # no old controller on port 6653
sudo rm -f /tmp/sdn_app_feedback.sock        # left by earlier sudo runs (tests, experiments)
python3 -m unittest discover -s tests        # 39 unit tests, should say OK
```
The controller must print `Listening for application QoS reports on /tmp/sdn_app_feedback.sock`. If it prints `Application feedback channel disabled ... Operation not permitted`, a file from an earlier `sudo` run is in the way: stop the controller, run the `sudo rm` line above, start it again (`run_controller.sh` now does this for you and may ask for your password).

The graphs and tables in `results/` come with the project. To show results measured **on your own laptop**, run the experiments the day before (about 25 minutes, the laptop must not sleep):
```bash
sudo ./run_all.sh                            # or: sudo ./run_all.sh --only flows   (one scenario)
```

Open these so they are ready to show: `results/comparison.md`, `results/linkfail_packets.png`, `results/flows.png`, `results/linkfail_throughput.png`, `results/sweep.png`, `results/congestion_throughput.png`, `tests/TEST_REPORT.md`.

Open **two terminals** in the project folder, side by side. Make the font a bit smaller so long lines fit.

## Part 1 – Start the system (1 min)

**Terminal 1 – controller**
```bash
./run_controller.sh app_aware
```
**Terminal 2 – network**
```bash
sudo python3 topology.py
```
```
mininet> pingall
```
`*** Results: 0% dropped (12/12 received)`. Say: 5 switches, 3 parallel paths P1/P2/P3 of 10 Mbps, clients on h1/h2, servers on h3/h4.

## Part 2 – Sequence numbers and timestamps of every packet (3 min)

*Rubric: Complete Application Functionality.* Every DATA packet carries a **sequence number** (bytes 8–11 of our header) and the **sender's timestamp** (bytes 12–19). Show both ends.

Start a server that prints every packet it receives, then a very slow client (2 packets per second, so you can read each one) that prints every packet it sends:
```
mininet> h4 python3 app/udp_server.py --port 5003 --out-dir /tmp/demo --show-packets > /tmp/pkts.log 2>&1 &
mininet> h1 python3 app/udp_client.py --server 10.0.0.4 --port 5003 --rate 0.02 --duration 3 --name P --show-packets
```
```
[client P] session accepted (reports every 500 ms)
[client P] streaming 0.02 Mbps (1200-byte packets, 2 pkt/s) for 3.0s to 10.0.0.4:5003
[pkt] P  sent DATA seq=0       timestamp=16:50:09.533604  (1200 bytes)
[pkt] P  sent DATA seq=1       timestamp=16:50:10.013696  (1200 bytes)
[pkt] P  sent DATA seq=2       timestamp=16:50:10.493675  (1200 bytes)
[client P] server sees 0.04 Mbps, loss 0.0%, delay 0.7 ms, jitter 0.01 ms
...
[client P] SUMMARY: sent 7, server received 7, lost 0 (0.00%), avg delay 0.8 ms, avg throughput 0.02 Mbps
```
Now the receiving side:
```
mininet> sh cat /tmp/pkts.log
```
```
[server 16:50:09] session P from 10.0.0.1:42010 started: 0.02 Mbps, 1200-byte packets (1 active)
[pkt] P  seq=0       sent=16:50:09.533604  recv=16:50:09.534193  delay=  0.59 ms
[pkt] P  seq=1       sent=16:50:10.013696  recv=16:50:10.014435  delay=  0.74 ms
[pkt] P  seq=2       sent=16:50:10.493675  recv=16:50:10.494372  delay=  0.70 ms
...
[server 16:50:12] session P finished: sent 7, received 7, lost 0 (0.00%), avg delay 0.8 ms, out of order 0, duplicates 0
```
**Say:** the `sent=` time on the server is exactly the `timestamp=` the client put into the packet, because the server reads it from the header. `delay` = receive time − sent time = one-way delay of that single packet (valid because all Mininet hosts share one clock). The sequence numbers 0, 1, 2 … have no gap, so nothing was lost. If one were missing, the server would print `<- GAP: seq … missing`, and a packet arriving after a higher one is printed as `<- LATE` (out of order).

## Part 3 – Increasing the number of flows (4 min)

*Rubric: Dynamic SDN Functionality (monitoring, rerouting, load balancing) and Complete Application Functionality (many clients).* One command starts two servers (h3 and h4, with a per-packet trace) and **8 flows of 2 Mbps**, one every 2 seconds, all starting on P1. Together they need 16 Mbps; one path carries only 10.
```
mininet> source demo/flows8.mn
```
```
*** F1: h1 to h3, 2 Mbps (UDP port 41001)
*** F2: h2 to h4, 2 Mbps (UDP port 41002)
...
*** F8: h2 to h3, 2 Mbps (UDP port 41008)
*** 8 flows running for about 5 minutes (16 Mbps in total; one path carries 10 Mbps)
```
(The lines like `[3] 18609` are just the job numbers of background commands.) It takes about 20–30 seconds in a VM; the flows then run for about 5 minutes. **Wait until the `mininet>` prompt is back before typing the next command** (anything typed earlier goes to the running command and is lost). **Point at Terminal 1** while it runs:
```
WARNING:congestion:CONGESTION on s1->s2: 8.39 Mbps (84%) flows=['h1:41001->h3:5001(F1)', 'h2:41002->h4:5002(F2)', 'h1:41003->h4:5002(F3)', 'h2:41004->h3:5001(F4)']
WARNING:congestion:REROUTE h2:41004->h3:5001(F4) (2.07 Mbps): P1 (via s2) -> P2 (via s3)  [congestion on s1->s2]
WARNING:congestion:REROUTE h1:41005->h3:5001(F5) (3.08 Mbps): P1 (via s2) -> P3 (via s4)  [congestion on s1->s2]
WARNING:congestion:REROUTE h1:41003->h4:5002(F3) (3.08 Mbps): P1 (via s2) -> P2 (via s3)  [congestion on s1->s2]
WARNING:congestion:REROUTE h2:41006->h4:5002(F6) (2.07 Mbps): P1 (via s2) -> P3 (via s4)  [congestion on s1->s2]
WARNING:congestion:REROUTE h2:41008->h3:5001(F8) (0.96 Mbps): P1 (via s2) -> P2 (via s3)  [congestion on s1->s2]
INFO:congestion:Congestion cleared on s1->s2 (6.20 Mbps)
```
**Say:** every new flow starts on P1. When P1 passes 80% the controller moves flows to the least-loaded path. When several flows arrived together it moves several in one decision, until P1 is under 80% again.

Which path is each flow on now?
```
mininet> sh python3 tools/show_paths.py
```
```
UDP flows on s1 (per-flow rules installed by the controller):
  flow                       path           traffic since the rule was installed
  h1:41001 -> h3:5001        P1 (via s2)      5.06 MB in   20 s (about 2.0 Mbps)
  h1:41007 -> h4:5002        P1 (via s2)      1.94 MB in    8 s (about 1.9 Mbps)
  h2:41002 -> h4:5002        P1 (via s2)      4.54 MB in   18 s (about 2.0 Mbps)
  h1:41003 -> h4:5002        P2 (via s3)      4.02 MB in   16 s (about 2.0 Mbps)
  h2:41004 -> h3:5001        P2 (via s3)      3.50 MB in   14 s (about 2.0 Mbps)
  h2:41008 -> h3:5001        P2 (via s3)      1.41 MB in    6 s (about 1.9 Mbps)
  h1:41005 -> h3:5001        P3 (via s4)      2.96 MB in   12 s (about 2.0 Mbps)
  h2:41006 -> h4:5002        P3 (via s4)      2.46 MB in   10 s (about 2.0 Mbps)
Flows per path:  P1: 3   P2: 3   P3: 2
```
**Say:** this reads the controller's per-flow rules on s1 (the same as `sh ovs-ofctl dump-flows s1 udp`, translated: output port 3 = P1, 4 = P2, 5 = P3). Every flow gets its full 2 Mbps. The source port tells you the flow: 41001 = F1 … 41008 = F8.

Now look at the packets **on the wire** at server h3, with a raw-socket sniffer (it prints every 100th DATA packet of each flow, plus the REPORT messages going back; in a VM it can take 10 seconds to start):
```
mininet> h3 python3 tools/packet_sniffer.py --port 5001 --count 300 --every 100
```
```
capturing on h3-eth0 (UDP port 5001) - stops after 300 datagrams or 30 s
   #  captured         source -> destination            type       session       seq  sender time          delay  notes
   1  16:50:41.919520  10.0.0.1:41001 -> 10.0.0.3:5001  DATA       3ea41824     4807  16:50:41.919196    0.32 ms  first DATA packet of this flow in the capture
   2  16:50:41.919940  10.0.0.1:41005 -> 10.0.0.3:5001  DATA       d583ee9f     3120  16:50:41.919178    0.76 ms  first DATA packet of this flow in the capture
   3  16:50:41.920136  10.0.0.2:41008 -> 10.0.0.3:5001  DATA       01f66d0a     1871  16:50:41.919541    0.59 ms  first DATA packet of this flow in the capture
   4  16:50:41.920925  10.0.0.2:41004 -> 10.0.0.3:5001  DATA       e13ef1f2     3550  16:50:41.920569    0.36 ms  first DATA packet of this flow in the capture
  69  16:50:42.000356  10.0.0.3:5001 -> 10.0.0.1:41001  REPORT     3ea41824        -  16:50:42.000276             server got 104 pkts, lost 0, 2.00 Mbps, delay 0.4 ms
 123  16:50:42.059084  10.0.0.2:41008 -> 10.0.0.3:5001  DATA       01f66d0a     1900  16:50:42.058839    0.24 ms
 208  16:50:42.161479  10.0.0.2:41004 -> 10.0.0.3:5001  DATA       e13ef1f2     3600  16:50:42.160489    0.99 ms

300 protocol datagrams decoded.
DATA 10.0.0.1:41001 -> 10.0.0.3:5001 (session 3ea41824): seq 4807..4880, 74 received, 0 missing (0 gaps), 0 out of order, 0 duplicates; delay avg 0.38 ms, p95 0.56 ms, max 0.72 ms
...
```
**Say:** this is not our server printing; it is a separate program that reads raw Ethernet frames from h3's interface (`AF_PACKET` raw socket), strips the Ethernet, IP and UDP headers and decodes our 20-byte header. Four flows arrive at h3 (F1, F4, F5, F8), each with its own session id and its own sequence numbers counting up. The REPORT packets go back to the clients every 0.5 s. (Add `--write /tmp/h3.pcap` to save a capture that opens in Wireshark.)

## Part 4 – Link failure while 8 flows run (3 min)

*Rubric: Challenging Scenario / Robustness.* Break the link to P2 while three flows use it:
```
mininet> link s1 s3 down
```
**Terminal 1** reacts at once:
```
WARNING:congestion:LINK DOWN s1-s3 (3 flows affected)
WARNING:congestion:REROUTE h2:41004->h3:5001(F4) (2.08 Mbps): P2 (via s3) -> P3 (via s4)  [link-failure on s1->s3]
WARNING:congestion:REROUTE h2:41008->h3:5001(F8) (2.08 Mbps): P2 (via s3) -> P3 (via s4)  [link-failure on s1->s3]
WARNING:congestion:REROUTE h1:41003->h4:5002(F3) (2.07 Mbps): P2 (via s3) -> P1 (via s2)  [link-failure on s1->s3]
WARNING:congestion:No better path available for congested link s1->s2 (all paths near the threshold)
```
```
mininet> sh python3 tools/show_paths.py
```
```
...
Flows per path:  P1: 4   P2: 0   P3: 4
```
**Say:** the switch reported the dead port (OpenFlow PORT_STATUS), the controller moved the three flows to the two working paths and split them 4 / 4. Both paths are now at about 83%, so the controller says there is no better path and leaves them there (moving more would only overload the other one). Repair the link:
```
mininet> link s1 s3 up
```
Terminal 1: `LINK UP s1-s3`. Now prove **packet by packet** what the failure cost, from the sequence numbers the servers recorded:
```
mininet> sh python3 tools/trace_summary.py --brief /tmp/d2/trace_*.csv
```
```
F1     8910 packets, seq 0..8909, lost 0, out of order 0, delay avg 0.9 / max 39.0 ms
F2     8494 packets, seq 0..8493, lost 0, out of order 0, delay avg 1.0 / max 36.3 ms
F3     8064 packets, seq 0..8073, lost 10 (seq 4885-4894 at 16:50:46.359806, 52 ms without packets), out of order 7, delay avg 1.0 / max 37.2 ms
F4     7647 packets, seq 0..7656, lost 10 (seq 4465-4474 at 16:50:46.361526, 52 ms without packets), out of order 0, delay avg 0.6 / max 7.9 ms
F5     7224 packets, seq 0..7223, lost 0, out of order 7, delay avg 1.1 / max 38.5 ms
F6     6824 packets, seq 0..6823, lost 0, out of order 7, delay avg 1.1 / max 37.7 ms
F7     6401 packets, seq 0..6400, lost 0, out of order 0, delay avg 0.4 / max 11.9 ms
F8     5969 packets, seq 0..5978, lost 10 (seq 2786-2795, ...), out of order 0, delay avg 0.5 / max 8.0 ms
```
**Say:**
- Only the three flows that were on P2 (F3, F4, F8) lost packets: **10 each**, all at the same instant (the moment of the failure), with **about 50 ms** without packets. Those are the packets that were already on the broken link. Every other flow lost nothing.
- *Out of order 7* on F3, F5, F6: when the controller moved these flows off the busy P1 earlier, the first packets on the new empty path overtook packets still waiting in P1's queue. They were not lost, just reordered, and the sequence numbers show it.
- With static routing the same failure loses **every** packet for the whole outage; see Part 5.

Finish the live part:
```
mininet> exit
```
Then press **Ctrl+C** in Terminal 1, and run `sudo mn -c`.

## Part 5 – Performance evaluation: static vs. SDN under 4 conditions (4 min)

*Rubric: Performance Evaluation & Comparison.* These come from `sudo ./run_all.sh` (every experiment is automated and repeatable). Open `results/comparison.md` and the graphs. **Baseline** = static routing (shortest path, never changes). **Proposed** = app-aware controller. Metrics: throughput, loss, one-way delay, jitter, detection/reroute/recovery time, fairness, and per packet: sequence-number gaps and time without packets.

**1. Congestion** (`results/congestion_throughput.png`, 3 flows of 6 Mbps)
- Static: **30% of all packets lost**, about 1 s of queueing delay; the third flow (C) gets only 0.24 of its 6 Mbps.
- Adaptive (switch statistics only): detects after 2.6 s, **0.73%** loss.
- App-aware: the application's delay report triggers the reroute after **0.7 s**, **0% loss**, 2–4 ms average delay.

**2. Link failure** (`results/linkfail_throughput.png`)
- Static: **96% loss** during the 12 s failure (flows stay on the dead link).
- App-aware: flows moved **3 ms** after the PORT_STATUS message, **0.36%** loss during the failure.

**3. The same link failure, packet by packet** (`results/linkfail_packets.png`). This is where the sequence numbers and timestamps pay off. Show it live in a normal terminal:
```bash
python3 tools/trace_summary.py results/linkfail/static/trace_A.csv --head 3
python3 tools/trace_summary.py results/linkfail/app_aware/trace_A.csv --head 0
```
```
Gaps in the sequence numbers: 1 gap, 3742 packets lost
  seq 3115-6856      3742 packets lost   last packet before: ... (t= 12.06 s)   next: ... (t= 24.03 s)   (11979 ms with nothing received)
```
```
Gaps in the sequence numbers: 1 gap, 14 packets lost
  seq 3113-3126        14 packets lost   last packet before: ... (t= 12.05 s)   next: ... (t= 12.10 s)   (48 ms with nothing received)
```
**Say:** static: the sequence numbers stop at t = 12.06 s and continue at 6857 when the link is repaired at t = 24.03 s, so 3742 packets in a row never arrived. SDN: only 14 packets are missing and the flow continues on the new path after 48 ms. The graph shows the same thing: a 12 s hole in the static line, an unbroken SDN line, and a zoom on the 14-packet gap. The bottom-right panel is the one-way delay of every packet, computed from the timestamps.

**4. Load sweep** (`results/sweep.png`, 2 flows on P1 from 62% to 145% of its capacity)
- Static is fine up to 83%, then breaks: 445 ms delay at 103%, **17% loss at 124%, 29% at 145%**.
- App-aware: at most 0.06% loss and under 2 ms delay at every load.

**5. Increasing number of flows** (`results/flows.png`, 1 to 12 flows of 2 Mbps)
- Static: all flows on P1, so delivered traffic stops at about **9.7 Mbps** from 6 flows on; loss grows **12% → 29% → 35% → 48%** (6, 8, 10, 12 flows), delay about 0.7–0.8 s, fairness drops to 0.77.
- App-aware: delivers **everything** (24 of 24 Mbps with 12 flows), loss at most 0.04%, average delay at most 2.3 ms, fairness 1.000; the flows end up 3/1/0, 3/2/1, 3/3/2, 3/4/3 and **4/4/4** on P1/P2/P3.

**If the invigilator wants to see an experiment run live** (after `exit`ing Mininet and stopping the controller; each takes about a minute and starts its own controller):
```bash
sudo python3 experiment.py --scenario linkfail --mode static           # the failure with static routing
python3 tools/trace_summary.py results/linkfail/static/trace_A.csv --head 3
sudo python3 experiment.py --scenario sweep --rate 6 --mode static     # one point of the load sweep (124 %)
sudo python3 experiment.py --scenario flows --flows 8 --mode static    # 8 flows, all stuck on P1
python3 analyze.py                                                     # tables and graphs updated
```

**Conclusion (one sentence):** under every condition we tried, the SDN controller turned heavy loss and second-long delays into (near) zero loss and millisecond delays, and the application feedback made it react almost 4 times faster than switch statistics alone (0.7 s instead of 2.6 s).

## Part 6 – Tests and documentation (1 min)

1. Open `tests/TEST_REPORT.md`: **14 test cases, all passed**. New for D2: **TC13** (sequence numbers and timestamps: client, server, trace file and on the wire) and **TC14** (8 flows spread over the three paths with 0% loss). To run the two live (about 45 s, in a normal terminal, after `exit`ing Mininet):
   ```bash
   sudo python3 tests/run_tests.py --only TC13 TC14
   ```
2. Show the README on GitHub: architecture diagrams, protocol, rubric mapping (§11), how to reproduce every result (§10).
3. Limitations (be honest, examiners like it): statistics are polled every 1 s; the topology is fixed in `netconfig.py`; one controller; a whole flow is moved, never split; new flows always start on P1; one-way delay works because all Mininet hosts share one clock.

## If something goes wrong during the demo

| Problem | Fix |
|---|---|
| Controller does not start (port 6653 busy) | `sudo pkill -f pox.py`, then `sudo systemctl stop openvswitch-testcontroller` |
| Mininet complains about existing interfaces | `exit`, `sudo mn -c`, start again |
| `source demo/flows8.mn` → `error reading file` | You started `topology.py` from another folder. `exit`, `cd ~/CN_Group3_Congestion_Rerouting`, start again |
| A server prints `Address already in use` | A server from an earlier try still runs: `mininet> sh pkill -f udp_server.py`, then repeat the step |
| `show_paths.py` shows no flows | The clients have finished (they run about 5 minutes). `source demo/flows8.mn` again (it stops the old servers and clients first) |
| Controller: `Application feedback channel disabled ... Operation not permitted` | Ctrl+C the controller, `sudo rm -f /tmp/sdn_app_feedback.sock`, start it again |
| Everything is slow, the controller keeps moving flows, delays of 100+ ms | The VM is short of CPU: give it 4 CPUs (see "Before the demo") |
| A command you typed vanished | It was typed before the `mininet>` prompt came back. Wait for the prompt and type it again |
| Nothing works | Show the recorded results (Part 5) and run `sudo python3 tests/run_tests.py --only TC10 TC13 TC14` to prove link-failure recovery, the packet trace and load balancing |
