# Installing and running the project in Mininet

This follows the course's *Mininet Installation Manual on Existing Linux VM (Ubuntu)* (UE25CS243A) and adds the few extra steps this project needs. You need an Ubuntu VM (20.04 / 22.04 preferred) in VirtualBox or VMware, with sudo rights and internet.

**VM settings (VM switched off):** 2 or more CPUs, 4 GB RAM, network adapter **NAT** or **Bridged**.

## Step 1 – Install Mininet (as in the course manual)

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install mininet -y              # installs Mininet + Open vSwitch
sudo apt install openvswitch-switch -y   # only if Open vSwitch errors appear
```

Verify, exactly as in the manual:
```bash
sudo mn
mininet> pingall          # expect 0% dropped
mininet> exit
sudo mn -c                # clean up
```

> If `sudo mn` says *"Could not find a default OpenFlow controller"*, run `sudo apt install openvswitch-testcontroller -y`, then **stop its service** so it does not occupy port 6653, which POX needs: `sudo systemctl stop openvswitch-testcontroller && sudo systemctl disable openvswitch-testcontroller`. Our project does not need it, because it brings its own controller.

## Step 2 – Install the project's extra tools

```bash
sudo apt install -y git python3-matplotlib
git clone https://github.com/noxrepo/pox.git ~/pox      # the SDN controller
python3 ~/pox/pox.py --version                           # prints: POX 0.7.0 (gar)
```
(POX may warn that it prefers Python 3.6–3.9. That is fine; it works on newer versions.)

## Step 3 – Get the project

From GitHub:
```bash
cd ~
git clone https://github.com/<your-username>/CN_Group3_Congestion_Rerouting.git
cd CN_Group3_Congestion_Rerouting
chmod +x *.sh
```
Or unzip the project into your home folder and `cd` into it.

Quick check that needs no Mininet:
```bash
python3 -m unittest discover -s tests      # 28 tests -> OK
```

## Step 4 – Live run in the Mininet CLI (what you show in the demo)

Open two terminals in the project folder.

**Terminal 1 – start the SDN controller**
```bash
./run_controller.sh app_aware
```
Leave it running. It prints every decision: new flows, congestion, reroutes, link failures.

**Terminal 2 – start the network**
```bash
sudo python3 topology.py
```
You get the `mininet>` prompt with our 5-switch, 4-host topology. **Type or paste one command at a time and wait for the `mininet>` prompt to come back.** If you paste several lines at once, Mininet can pass some of them to the programs running on the hosts instead of executing them.

These are the same commands as in the course manual:
```
mininet> nodes
mininet> net
mininet> dump
mininet> pingall
mininet> sh ovs-ofctl dump-flows --no-stats s1   # rules the controller installed
```

Start the UDP servers and the first client. Output goes to log files so the CLI stays readable:
```
mininet> h3 python3 app/udp_server.py --port 5001 --out-dir /tmp/demo > /tmp/demo_h3.log 2>&1 &
mininet> h4 python3 app/udp_server.py --port 5002 --out-dir /tmp/demo > /tmp/demo_h4.log 2>&1 &
mininet> h1 python3 app/udp_client.py --server 10.0.0.3 --port 5001 --rate 6 --duration 90 --name A --sport 40001 > /tmp/demo_A.log 2>&1 &
mininet> sh ovs-ofctl dump-flows --no-stats s1 udp      # flow A: actions=output:3  -> path P1 (via s2)
```

Create congestion. A second 6 Mbps flow puts 12 Mbps onto the 10 Mbps path P1:
```
mininet> h2 python3 app/udp_client.py --server 10.0.0.4 --port 5002 --rate 6 --duration 80 --name B --sport 40002 > /tmp/demo_B.log 2>&1 &
```
Terminal 1 now shows `CONGESTION on s1->s2 ...` and `REROUTE ...(B) ... P1 (via s2) -> P2 (via s3)`.
```
mininet> sh ovs-ofctl dump-flows --no-stats s1 udp      # flow B: actions=output:4  -> path P2 (via s3)
```

Fail a link:
```
mininet> link s1 s3 down                     # Terminal 1: LINK DOWN, B moved P2 -> P3
mininet> sh ovs-ofctl dump-flows --no-stats s1 udp      # flow B: actions=output:5  -> path P3 (via s4)
mininet> pingall                             # still 0% dropped: default routes avoid the failed link
mininet> link s1 s3 up
```

See what the application measured:
```
mininet> sh tail -n 3 /tmp/demo_A.log /tmp/demo_B.log
mininet> sh cat /tmp/demo/flow_A.csv | tail
mininet> exit
```
Then press Ctrl+C in Terminal 1 to stop the controller, and run `sudo mn -c`.

To see the **baseline**, repeat with `./run_controller.sh static` in Terminal 1. Congestion is detected, but nothing is moved and loss stays high.

`--no-stats` hides the packet counters so the rules fit on one line (you can leave it out).

Ports on s1: `1` = h1, `2` = h2, `3` = s2 (P1), `4` = s3 (P2), `5` = s4 (P3). Depending on the Open vSwitch version, `dump-flows` shows them as numbers (`output:4`) or names (`output:"s1-eth4"`); both mean the same.

When you `exit` Mininet while the controller is still running, the controller logs `LINK DOWN` for every link. That is just the network being torn down.

**Demo 2 additions** (packet view, many flows, link failure under load) are in [DEMO2_SCRIPT.md](DEMO2_SCRIPT.md). The short version:
```
mininet> h4 python3 app/udp_server.py --port 5003 --out-dir /tmp/demo --show-packets > /tmp/pkts.log 2>&1 &
mininet> h1 python3 app/udp_client.py --server 10.0.0.4 --port 5003 --rate 0.02 --duration 3 --name P --show-packets
mininet> sh cat /tmp/pkts.log
mininet> source demo/flows8.mn
mininet> sh python3 tools/show_paths.py
mininet> h3 python3 tools/packet_sniffer.py --port 5001 --count 300 --every 100
mininet> link s1 s3 down
mininet> sh python3 tools/show_paths.py
mininet> link s1 s3 up
mininet> sh python3 tools/trace_summary.py --brief /tmp/d2/trace_*.csv
```

## Step 5 – Automated experiments (for the results and graphs)

You do **not** need Terminal 1 here. Each experiment starts and stops its own controller.
```bash
sudo ./run_all.sh --quick       # congestion scenario, 3 modes, about 4 minutes
sudo ./run_all.sh               # + link failure + load sweep + 1-12 flows, about 25 minutes
sudo ./run_all.sh --only flows  # just one scenario: congestion | linkfail | sweep | flows
```
Or run a single experiment:
```bash
sudo python3 experiment.py --scenario congestion --mode app_aware
sudo python3 experiment.py --scenario linkfail   --mode static
sudo python3 experiment.py --scenario sweep --rate 5 --mode app_aware
sudo python3 experiment.py --scenario flows --flows 8 --mode app_aware
python3 analyze.py              # tables + graphs from whatever has been run
python3 tools/trace_summary.py results/linkfail/static/trace_A.csv   # packet-level view
```
Results go to `results/` (`comparison.md` and the `.png` graphs).

## Step 6 – Automated test cases

```bash
sudo python3 tests/run_tests.py            # 14 test cases, about 5 minutes
sudo python3 tests/run_tests.py --list     # what each test checks
sudo python3 tests/run_tests.py --only TC08 TC10
```
The report is written to `tests/TEST_REPORT.md`, and the logs to `tests/logs/<TC>/`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Port 6653 is busy` / `Address already in use` | `sudo pkill -f pox.py`. If it persists: `sudo systemctl stop openvswitch-testcontroller` |
| `Switches did not connect` | Start the controller first (Terminal 1), or let `experiment.py` start it (don't use `--no-controller`) |
| `RTNETLINK answers: File exists`, or Mininet won't start | `sudo mn -c` |
| `POX not found` | `git clone https://github.com/noxrepo/pox.git ~/pox`, or set `POX_DIR=/path/to/pox` |
| `ovs-vsctl: unix:/var/run/openvswitch/db.sock ... connection failed` | `sudo service openvswitch-switch start` |
| Graphs not created | `sudo apt install python3-matplotlib` |
| No `openvswitch` kernel module (WSL, Docker) | Add `--userspace` to `topology.py`, `experiment.py`, `run_all.sh` or `run_tests.py`. A normal Ubuntu VM does not need this |
| `*** Error: Warning: sch_htb: quantum of class 50001 is big. Consider r2q change.` | Only a warning from Linux traffic shaping, not an error; tests still pass. Fixed from this version on: host links are no longer shaped (`HOST_LINK_BW_MBPS = None` in `netconfig.py`) |
| `Unable to contact the remote controller at 127.0.0.1:6653` | Harmless if the switches connect afterwards (Mininet checks with `telnet` before the controller is ready, or when telnet is not installed) |
| A client prints `server unreachable` | Start the server first, and check the IP/port (h3 = 10.0.0.3:5001, h4 = 10.0.0.4:5002) |
| `packet_sniffer.py`: `raw sockets need root` | Run it from the Mininet CLI (`h3 python3 tools/packet_sniffer.py ...`), which is already root |
| `source demo/flows8.mn` says `error reading file` | Start `topology.py` from the project folder, so the relative path works |
| Controller: `Application feedback channel disabled ... Operation not permitted` | An earlier `sudo` run left `/tmp/sdn_app_feedback.sock`: `sudo rm -f /tmp/sdn_app_feedback.sock`, restart the controller |
| Demo with many flows is slow, delays of 100+ ms | Give the VM 4 CPUs (VirtualBox → Settings → System → Processor, VM shut down) |
| A server prints `Address already in use` | A server from an earlier step still uses that port: pick another port, or `sh pkill -f udp_server.py` first |
