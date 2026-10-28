#!/usr/bin/env python3
"""
topology.py - Multi-path Mininet topology for the congestion-rerouting project.

Run interactively (controller must already be running in another terminal):

    sudo python3 topology.py

You get the Mininet CLI, where you can try for example:

    mininet> pingall
    mininet> h3 python3 traffic/udp_receiver.py --port 5001 --duration 60 --out /tmp/r1.csv &
    mininet> h1 python3 traffic/udp_sender.py --dst 10.0.0.3 --port 5001 --rate 6 --duration 50 &
    mininet> sh ovs-ofctl dump-flows s1          # watch the per-flow rules change

The automated experiment (experiment.py) builds this same topology.
"""

import argparse
import os
import sys
from functools import partial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import netconfig as nc  # noqa: E402

from mininet.topo import Topo  # noqa: E402
from mininet.net import Mininet  # noqa: E402
from mininet.node import OVSSwitch, RemoteController  # noqa: E402
from mininet.link import TCLink  # noqa: E402
from mininet.cli import CLI  # noqa: E402
from mininet.log import setLogLevel, info  # noqa: E402


class MultiPathTopo(Topo):
    """Two edge switches joined by three parallel 2-hop paths (see netconfig.py)."""

    def build(self):
        for name, dpid in nc.SWITCHES.items():
            # dpid must be a 16-hex-digit string in Mininet
            self.addSwitch(name, dpid="%016x" % dpid, protocols="OpenFlow10")

        for name, h in nc.HOSTS.items():
            self.addHost(name, ip=h["ip"] + "/24", mac=h["mac"])
            host_link = {"bw": nc.HOST_LINK_BW_MBPS} if nc.HOST_LINK_BW_MBPS else {}
            self.addLink(name, h["switch"], port2=h["port"], **host_link)

        core = {"bw": nc.CORE_LINK_BW_MBPS}
        if nc.CORE_LINK_MAX_QUEUE:
            core["max_queue_size"] = nc.CORE_LINK_MAX_QUEUE
        for a, pa, b, pb in nc.SWITCH_LINKS:
            self.addLink(a, b, port1=pa, port2=pb, **core)


def build_network(userspace=False, controller_ip=nc.CONTROLLER_IP,
                  controller_port=nc.CONTROLLER_PORT):
    """Create (but do not start) the Mininet network.

    userspace=True runs Open vSwitch in userspace (netdev datapath). Only
    needed on machines where the openvswitch kernel module is unavailable
    (e.g. some containers / WSL). Normal Mininet VMs do not need it.
    """
    switch_cls = partial(OVSSwitch, datapath="user") if userspace else OVSSwitch
    net = Mininet(
        topo=MultiPathTopo(),
        switch=switch_cls,
        link=TCLink,
        controller=None,
        autoSetMacs=False,
        autoStaticArp=False,
    )
    net.addController("c0", controller=RemoteController,
                      ip=controller_ip, port=controller_port)
    return net


def prepare_hosts(net, userspace=False):
    """Disable IPv6 on hosts so the controller only sees the traffic we generate.

    With the userspace datapath, OVS forwards packets whose UDP checksum was
    left for the NIC to fill in ("TX checksum offload"); the receiving host
    would then drop them as corrupt. Turning the offload off on the hosts
    makes the kernel compute real checksums. (Not needed with kernel OVS.)
    """
    for h in net.hosts:
        for key in ("all", "default", "lo"):
            h.cmd("sysctl -qw net.ipv6.conf.%s.disable_ipv6=1" % key)
        for intf in h.intfNames():
            h.cmd("sysctl -qw net.ipv6.conf.%s.disable_ipv6=1" % intf)
            if userspace:
                h.cmd("ethtool -K %s tx off" % intf)


def main():
    parser = argparse.ArgumentParser(description="Start the multipath topology with a CLI")
    parser.add_argument("--controller-ip", default=nc.CONTROLLER_IP)
    parser.add_argument("--controller-port", type=int, default=nc.CONTROLLER_PORT)
    parser.add_argument("--userspace", action="store_true",
                        help="use OVS userspace datapath (only if kernel module missing)")
    args = parser.parse_args()

    setLogLevel("info")
    net = build_network(args.userspace, args.controller_ip, args.controller_port)
    net.start()
    prepare_hosts(net, args.userspace)
    info("*** Waiting for switches to connect to the controller\n")
    net.waitConnected(timeout=20)
    info("\n*** Topology ready. Paths s1->s5:\n")
    for p in nc.all_paths("s1", "s5"):
        info("    %-14s %s\n" % (nc.path_name(p), " -> ".join(p)))
    info("*** Core links: %d Mbps each, host links: %s\n"
         % (nc.CORE_LINK_BW_MBPS, "%d Mbps" % nc.HOST_LINK_BW_MBPS
            if nc.HOST_LINK_BW_MBPS else "not limited"))
    CLI(net)
    net.stop()


if __name__ == "__main__":
    main()
