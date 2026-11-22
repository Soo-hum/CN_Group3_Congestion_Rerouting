# Demo 1 script (D1, about 12 minutes)

For the final demo (D2: link failure, many flows, sequence numbers and timestamps, performance comparison) see **[DEMO2_SCRIPT.md](DEMO2_SCRIPT.md)**.

Suggested split. Change the names as you like, but **every member should be able to run the demo and answer any viva question** (see `VIVA.md`). The rubric marks individual understanding.

| Part | Presenter | Time | Rubric component |
|---|---|---|---|
| 1. Problem, architecture, topology | Member 1 (Samhita) | 3 min | Problem Understanding & Architecture |
| 2. Socket application + SDN controller, live demo | Member 2 (Sohum) | 5 min | Socket implementation, Mininet + SDN, Socket–SDN integration, Dynamic SDN, Robustness |
| 3. Test cases and performance comparison | Member 3 (Janhavi) | 4 min | Initial Testing, Performance Evaluation & Comparison |

## Before the demo (do this 10 minutes early)

```bash
cd ~/CN_Group3_Congestion_Rerouting
sudo mn -c                                   # clean up anything left over
sudo pkill -f pox.py                         # no old controller on port 6653
python3 -m unittest discover -s tests        # 39 unit tests, should say OK
```
Open these files in the image viewer or browser so they are ready to show: `results/congestion_throughput.png`, `results/congestion_paths.png`, `results/linkfail_throughput.png`, `results/sweep.png`, `results/comparison.md`, `tests/TEST_REPORT.md`.

Open **two terminals** in the project folder, side by side.

## Part 1 – Problem and architecture (Member 1)

1. Problem: UDP has no congestion control. When too much UDP traffic shares one path, packets queue up and are dropped, while other paths sit idle. The goal is for an SDN controller to **detect** this and **move traffic** to a less-loaded path.
2. Show the topology diagram in the README: 3 equal paths P1/P2/P3, 10 Mbps core links, clients on h1/h2, servers on h3/h4.
3. Show the architecture diagram: Mininet, the POX controller (OpenFlow 1.0), the UDP application, and the feedback channel from the server to the controller.
4. Walk through the sequence diagram: HELLO → PACKET_IN → per-flow rule → DATA/REPORT → statistics → reroute.
5. Expected behaviour: static routing keeps everything on P1. Our controller spreads flows over P1/P2/P3 and recovers from link failures.

## Part 2 – Live demo (Member 2)

**Terminal 1 – controller**
```bash
./run_controller.sh app_aware
```
Point at: `Switch s1 ... connected, base rules installed` (5 switches), and `Listening for application QoS reports`.

**Terminal 2 – network** (type one command at a time; wait for the `mininet>` prompt before the next one)
```bash
sudo python3 topology.py
```
```
mininet> net
mininet> pingall
mininet> sh ovs-ofctl dump-flows --no-stats s1
```
Explain the base rules: priority 100 default routes, 200 for ARP and new UDP flows going to the controller, 0 to drop everything else (no flooding in a looped topology).

Start the servers, then the first client:
```
mininet> h3 python3 app/udp_server.py --port 5001 --out-dir /tmp/demo > /tmp/demo_h3.log 2>&1 &
mininet> h4 python3 app/udp_server.py --port 5002 --out-dir /tmp/demo > /tmp/demo_h4.log 2>&1 &
mininet> h1 python3 app/udp_client.py --server 10.0.0.3 --port 5001 --rate 6 --duration 90 --name A --sport 40001 > /tmp/demo_A.log 2>&1 &
mininet> sh ovs-ofctl dump-flows --no-stats s1 udp
```
Point at the new `priority=300 ... tp_src=40001 ... actions=output:3` rule (some versions print `output:"s1-eth3"`). Port 3 leads to s2, so this is path P1.

Create congestion with a second 6 Mbps flow (12 Mbps on a 10 Mbps path):
```
mininet> h2 python3 app/udp_client.py --server 10.0.0.4 --port 5002 --rate 6 --duration 80 --name B --sport 40002 > /tmp/demo_B.log 2>&1 &
```
In **Terminal 1**, within about a second you see:
```
WARNING:congestion:CONGESTION on s1->s2 reported by application (... delay 34 ms ...)
WARNING:congestion:REROUTE h2:40002->h4:5002(B) (6.21 Mbps): P1 (via s2) -> P2 (via s3)
```
```
mininet> sh ovs-ofctl dump-flows --no-stats s1 udp
```
Flow B now goes out `output:4`, which leads to s3 (P2). That is the rerouting.

Link failure (challenging scenario):
```
mininet> link s1 s3 down
```
Terminal 1 shows `LINK DOWN s1-s3` and `REROUTE ... B ... P2 -> P3 [link-failure ...]`.
```
mininet> sh ovs-ofctl dump-flows --no-stats s1 udp
mininet> link s1 s3 up
mininet> sh tail -n 3 /tmp/demo_A.log /tmp/demo_B.log
mininet> exit
```
`tail` shows what the server reports to each client: throughput about 6 Mbps, loss about 0%.

## Part 3 – Tests and results (Member 3)

1. Open `tests/TEST_REPORT.md`: 14 automated test cases (connectivity, flow rules, sockets, concurrency, overload, error handling, congestion, static baseline, link failure, feedback channel, server restart, packet trace, 8 flows). Say which rubric area each one covers.
2. Open `results/congestion_throughput.png`. Static keeps losing packets (30% overall). Adaptive detects after 2.6 s and recovers within about 4 s. App-aware reacts after 0.7 s with 0% loss.
3. Open `results/congestion_paths.png`. Static has P1 at 100% while P2 and P3 sit at 0. The SDN modes spread the load. The second time, the controller picks **P3, because P2 already carries a flow**.
4. Open `results/linkfail_throughput.png`. Static loses everything for 12 s; app-aware moves both flows in milliseconds.
5. Open `results/sweep.png`. As the load grows from 62% to 145% of capacity, static loss and delay climb sharply while SDN rerouting stays near 0.
6. Conclusion: the numbers from `results/comparison.md`, plus the limitations (polling interval, fixed topology, single controller).

## If something goes wrong during the demo

- The controller does not start (port busy): `sudo pkill -f pox.py`, then `sudo systemctl stop openvswitch-testcontroller`.
- Mininet complains about existing interfaces: `exit`, then `sudo mn -c`, then start again.
- Nothing works: show the recorded results (`results/*.png`, `results/comparison.md`, `tests/TEST_REPORT.md`) and run `sudo python3 tests/run_tests.py --only TC08` (about 25 s) to prove rerouting works.
