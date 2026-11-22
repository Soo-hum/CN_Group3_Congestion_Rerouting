"""
congestion_controller.py - POX SDN controller that detects congestion on UDP
paths and dynamically reroutes flows to a less-loaded path.

Computer Networks mini project - Section H, Group 3, Problem 27
"Congestion Detection and Traffic Rerouting" (protocol: UDP)

Modes (--mode)
    static     baseline: flows stay on the default path P1; congestion and link
               failures are only logged
    adaptive   reroutes flows away from congested links (OpenFlow port/flow
               statistics) and away from failed links (OpenFlow PORT_STATUS)
    app_aware  adaptive + Socket-SDN integration: the UDP server reports the
               QoS its sessions experience (delay / loss) and the rate each
               client asked for; the controller uses this to react earlier and
               to know a new flow's demand before it can measure it

What happens
 1. Switch connects  -> base rules: default route per host IP (prio 100),
                        ARP and new UDP flows -> controller (prio 200),
                        drop everything else (prio 0)
 2. New UDP flow     -> per-flow rules (src/dst IP + ports, prio 300) on the
                        default path, installed egress-first
 3. Every interval   -> port stats (link load) + flow stats (flow rates)
 4. Link >= threshold for `consecutive` polls, or an application QoS alert
    while a link on the flow's path is over the threshold -> CONGESTED
 5. Reroute one flow to the least-loaded path (make-before-break), then a
    cool-down on the affected links
 6. PORT_STATUS link down -> every flow on that link is moved at once and the
    default routes are recomputed around the failed link

Logs (log_dir): events.csv, link_stats.csv, flow_stats.csv, app_qos.csv

Run from the POX directory (see run_controller.sh):
    PYTHONPATH=<project>/controller python3 pox.py openflow.of_01 --port=6653 \
        congestion_controller --mode=app_aware
"""

import csv
import json
import os
import socket
import sys
import threading
import time

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.packet.ethernet import ethernet
from pox.lib.packet.arp import arp
from pox.lib.addresses import IPAddr, EthAddr
from pox.lib.recoco import Timer

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
for p in (HERE, PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
import netconfig as nc  # noqa: E402
from routing_logic import order_flows_to_move, choose_path  # noqa: E402

log = core.getLogger("congestion")

PRIO_DROP = 0
PRIO_DEFAULT_ROUTE = 100
PRIO_TO_CONTROLLER = 200
PRIO_FLOW = 300
FLOW_IDLE_TIMEOUT = 10
MODES = ("static", "adaptive", "app_aware")


def _ip(addr):
    return str(addr).split("/")[0]


class Flow(object):
    """State the controller keeps for one UDP flow (one direction)."""

    def __init__(self, key, src_host, dst_host, path, now):
        self.key = key                    # (src_ip, dst_ip, src_port, dst_port)
        self.src_host, self.dst_host = src_host, dst_host
        self.path = list(path)
        self.rate_mbps = 0.0              # measured at the ingress switch
        self.measured = False
        self.last_bytes = self.last_time = None
        self.created = now
        self.last_moved = 0.0
        self.moves = 0
        self.app_name = None              # from the application (app_aware)
        self.app_demand = None            # Mbps on the wire the client asked for
        self.app_degraded = False

    @property
    def name(self):
        n = "%s:%d->%s:%d" % (self.src_host, self.key[2], self.dst_host, self.key[3])
        return "%s(%s)" % (n, self.app_name) if self.app_name else n

    def links(self):
        return nc.path_links(self.path)


class CongestionController(object):

    def __init__(self, mode, threshold, interval, consecutive, cooldown, hold,
                 delay_alert, loss_alert, feedback_socket, log_dir):
        self.mode = mode
        self.threshold = threshold
        self.clear_threshold = max(0.0, threshold - 0.1)
        self.interval = interval
        self.consecutive = consecutive
        self.cooldown = cooldown
        self.hold = hold
        self.delay_alert = delay_alert
        self.loss_alert = loss_alert
        self.capacity = float(nc.CORE_LINK_BW_MBPS)

        self.ports = nc.port_map()
        self.port_to_link = {(a, p): (a, b) for (a, b), p in self.ports.items()}
        self.connections = {}
        self.flows = {}
        self.app_sessions = {}            # flow key -> session_start message
        self.link_rate = {}
        self.port_prev = {}
        self.over_count = {}
        self.congested = set()
        self.no_path_warned = set()      # links already reported as "no better path"
        self.link_cooldown = {}
        self.down_links = set()
        self.tick_no = 0

        os.makedirs(log_dir, exist_ok=True)
        self.log_dir = log_dir
        self._events = self._open_csv("events.csv", ["time", "event", "link", "flow", "from_path",
                                                     "to_path", "load_mbps", "utilization",
                                                     "detail"])
        self._linkcsv = self._open_csv("link_stats.csv", ["time", "link", "mbps", "utilization"])
        self._flowcsv = self._open_csv("flow_stats.csv", ["time", "flow", "path", "mbps"])
        self._appcsv = self._open_csv("app_qos.csv", ["time", "name", "flow", "throughput_mbps",
                                                      "loss_pct", "delay_ms", "jitter_ms"])

        core.openflow.addListeners(self)
        Timer(self.interval, self._tick, recurring=True)
        self._start_feedback_listener(feedback_socket)
        log.info("Controller started: mode=%s threshold=%.0f%% of %.0f Mbps, poll=%.1fs, "
                 "consecutive=%d, cooldown=%.1fs, hold=%.1fs, app alerts: delay>=%.0fms "
                 "or loss>=%.1f%%, logs=%s", mode, threshold * 100, self.capacity, interval,
                 consecutive, cooldown, hold, delay_alert, loss_alert, log_dir)
        self._event("CONTROLLER_START", detail="mode=%s threshold=%.2f" % (mode, threshold))

    # ================================================================ logging
    def _open_csv(self, name, header):
        f = open(os.path.join(self.log_dir, name), "w", newline="")
        w = csv.writer(f)
        w.writerow(header)
        f.flush()
        return (f, w)

    def _write(self, target, row):
        f, w = target
        w.writerow(row)
        f.flush()

    def _event(self, event, link=None, flow=None, from_path=None, to_path=None,
               load=None, detail=""):
        self._write(self._events, [
            "%.3f" % time.time(), event,
            "%s->%s" % link if link else "",
            flow.name if flow else "",
            nc.path_name(from_path) if from_path else "",
            nc.path_name(to_path) if to_path else "",
            "%.2f" % load if load is not None else "",
            "%.2f" % (load / self.capacity) if load is not None else "",
            detail])

    # ================================================== application feedback
    def _start_feedback_listener(self, path):
        """Socket-SDN integration: a UNIX datagram socket where the UDP server
        reports what the application experiences. Received in a thread, then
        handed to POX's own thread with core.callLater (POX is not thread-safe)."""
        if not path or path.lower() == "none":
            return
        try:
            if os.path.exists(path):
                os.unlink(path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(path)
            os.chmod(path, 0o777)
        except OSError as e:
            log.warning("Application feedback channel disabled (%s)%s", e,
                        " - an earlier sudo run left this file; remove it with: "
                        "sudo rm -f %s, then restart the controller" % path
                        if isinstance(e, PermissionError) else "")
            return
        self._fb_sock, self._fb_path = sock, path

        def listen():
            while True:
                try:
                    data = sock.recv(65535)
                    msg = json.loads(data.decode())
                except (OSError, ValueError):
                    if getattr(self, "_fb_closed", False):
                        return
                    continue
                core.callLater(self._on_app_message, msg)

        threading.Thread(target=listen, name="app-feedback", daemon=True).start()
        core.addListenerByName("GoingDownEvent", self._close_feedback)
        log.info("Listening for application QoS reports on %s", path)

    def _close_feedback(self, event):
        self._fb_closed = True
        try:
            self._fb_sock.close()
            os.unlink(self._fb_path)
        except OSError:
            pass

    def _on_app_message(self, msg):
        try:
            cip, cport, sip, sport = msg["flow"]
            key = (cip, sip, int(cport), int(sport))
        except (KeyError, ValueError, TypeError):
            return
        kind = msg.get("type")
        flow = self.flows.get(key)
        now = time.time()
        if kind == "session_start":
            self.app_sessions[key] = msg
            if flow is not None:
                self._attach_app_info(flow)
            self._event("APP_SESSION_START", flow=flow,
                        detail="name=%s rate=%.2fMbps" % (msg.get("name"), msg.get("rate_mbps", 0)))
        elif kind == "session_end":
            self._event("APP_SESSION_END", flow=flow,
                        detail="name=%s reason=%s" % (msg.get("name"), msg.get("reason")))
        elif kind == "qos":
            delay, loss = msg.get("delay_ms", 0.0), msg.get("loss_pct", 0.0)
            self._write(self._appcsv, ["%.3f" % msg.get("t", now), msg.get("name"),
                                       "%s:%s->%s:%s" % (cip, cport, sip, sport),
                                       "%.3f" % msg.get("throughput_mbps", 0), "%.2f" % loss,
                                       "%.2f" % delay, "%.3f" % msg.get("jitter_ms", 0)])
            if flow is None:
                return
            degraded = delay >= self.delay_alert or loss >= self.loss_alert
            if degraded and not flow.app_degraded:
                flow.app_degraded = True
                self._event("APP_QOS_ALERT", flow=flow, from_path=flow.path,
                            detail="delay=%.1fms loss=%.1f%%" % (delay, loss))
            elif not degraded and flow.app_degraded:
                flow.app_degraded = False
                self._event("APP_QOS_OK", flow=flow, from_path=flow.path,
                            detail="delay=%.1fms loss=%.1f%%" % (delay, loss))
            if degraded and self.mode == "app_aware":
                self._app_triggered_check(flow, delay, loss, now)

    def _attach_app_info(self, flow):
        msg = self.app_sessions.get(flow.key)
        if msg:
            size = float(msg.get("size", 1200)) or 1200.0
            flow.app_name = msg.get("name")
            # the client asks for payload Mbps; on the wire add UDP+IP+Ethernet headers
            flow.app_demand = float(msg.get("rate_mbps", 0.0)) * (size + 42) / size

    def _app_triggered_check(self, flow, delay, loss, now):
        """The application sees bad QoS: if a link on its path is over the
        threshold, treat it as congested right away (no need to wait for
        `consecutive` polls)."""
        links = flow.links()
        if not links:
            return
        worst = max(links, key=self._link_load)
        load = self._link_load(worst)
        if load < self.threshold * self.capacity or now < self.link_cooldown.get(worst, 0.0):
            return
        if worst not in self.congested:
            self.congested.add(worst)
            names = [f.name for f in self.flows.values() if worst in f.links()]
            log.warning("CONGESTION on %s->%s reported by application (%s: delay %.0f ms, "
                        "loss %.1f%%), link load %.2f Mbps", worst[0], worst[1], flow.name,
                        delay, loss, load)
            self._event("CONGESTION_DETECTED", link=worst, load=load,
                        detail="source=application flow=%s delay=%.1fms loss=%.1f%% flows=%s"
                        % (flow.name, delay, loss, ";".join(names)))
        self._relieve(worst, load, now)

    # ========================================================= switch set-up
    def _handle_ConnectionUp(self, event):
        sw = nc.DPID_TO_NAME.get(event.dpid)
        if sw is None:
            log.warning("Unknown switch dpid=%s connected - ignoring", event.dpid)
            return
        self.connections[sw] = event.connection
        self._install_base_rules(sw, event.connection)
        log.info("Switch %s (dpid %d) connected, base rules installed", sw, event.dpid)
        self._event("SWITCH_UP", detail=sw)

    def _handle_ConnectionDown(self, event):
        sw = nc.DPID_TO_NAME.get(event.dpid)
        self.connections.pop(sw, None)
        log.info("Switch %s disconnected", sw)

    def _usable(self, path):
        return not any(l in self.down_links for l in nc.path_links(path))

    def _default_path(self, src_sw, dst_sw):
        """Shortest path; adaptive modes skip paths with failed links."""
        paths = nc.all_paths(src_sw, dst_sw)
        if self.mode != "static":
            usable = [p for p in paths if self._usable(p)]
            if usable:
                return usable[0]
        return paths[0]

    def _next_hop_port(self, sw, host):
        h = nc.HOSTS[host]
        if sw == h["switch"]:
            return h["port"]
        path = self._default_path(sw, h["switch"])
        return self.ports[(sw, path[1])]

    def _install_default_routes(self, sw, con):
        for host, h in nc.HOSTS.items():
            fm = of.ofp_flow_mod(priority=PRIO_DEFAULT_ROUTE,
                                 match=of.ofp_match(dl_type=0x0800, nw_dst=IPAddr(h["ip"])))
            fm.actions.append(of.ofp_action_output(port=self._next_hop_port(sw, host)))
            con.send(fm)

    def _install_base_rules(self, sw, con):
        con.send(of.ofp_flow_mod(command=of.OFPFC_DELETE))
        con.send(of.ofp_flow_mod(priority=PRIO_DROP, match=of.ofp_match()))
        self._install_default_routes(sw, con)
        fm = of.ofp_flow_mod(priority=PRIO_TO_CONTROLLER, match=of.ofp_match(dl_type=0x0806))
        fm.actions.append(of.ofp_action_output(port=of.OFPP_CONTROLLER))
        con.send(fm)
        for host, h in nc.HOSTS.items():
            if h["switch"] != sw:
                continue
            fm = of.ofp_flow_mod(priority=PRIO_TO_CONTROLLER,
                                 match=of.ofp_match(in_port=h["port"], dl_type=0x0800,
                                                    nw_proto=17))
            fm.actions.append(of.ofp_action_output(port=of.OFPP_CONTROLLER))
            con.send(fm)

    # ============================================================= packet-in
    def _handle_PacketIn(self, event):
        packet = event.parsed
        if not packet.parsed:
            return
        sw = nc.DPID_TO_NAME.get(event.dpid)
        a = packet.find("arp")
        if a is not None:
            self._proxy_arp(event, a)
            return
        ip = packet.find("ipv4")
        u = packet.find("udp")
        if ip is not None and u is not None:
            self._new_udp_packet(event, sw, ip, u)

    def _proxy_arp(self, event, a):
        if a.opcode != arp.REQUEST:
            return
        host = nc.IP_TO_HOST.get(str(a.protodst))
        if host is None:
            return
        reply = arp()
        reply.opcode = arp.REPLY
        reply.hwsrc = EthAddr(nc.HOSTS[host]["mac"])
        reply.hwdst = a.hwsrc
        reply.protosrc = a.protodst
        reply.protodst = a.protosrc
        eth = ethernet(type=ethernet.ARP_TYPE, src=reply.hwsrc, dst=a.hwsrc)
        eth.payload = reply
        po = of.ofp_packet_out(data=eth.pack(), in_port=event.port)
        po.actions.append(of.ofp_action_output(port=of.OFPP_IN_PORT))
        event.connection.send(po)

    def _new_udp_packet(self, event, sw, ip, u):
        key = (str(ip.srcip), str(ip.dstip), u.srcport, u.dstport)
        src_h = nc.IP_TO_HOST.get(key[0])
        dst_h = nc.IP_TO_HOST.get(key[1])
        if src_h is None or dst_h is None:
            return
        flow = self.flows.get(key)
        if flow is None:
            path = self._default_path(nc.HOSTS[src_h]["switch"], nc.HOSTS[dst_h]["switch"])
            flow = Flow(key, src_h, dst_h, path, time.time())
            self.flows[key] = flow
            self._attach_app_info(flow)
            self._install_flow_path(flow, path)
            log.info("New UDP flow %s on %s", flow.name, nc.path_name(path))
            self._event("FLOW_ADDED", flow=flow, to_path=path)
        po = of.ofp_packet_out(data=event.ofp)
        po.actions.append(of.ofp_action_output(port=self._out_port(sw, flow.path, flow.dst_host)))
        event.connection.send(po)

    # ============================================================ flow rules
    def _out_port(self, sw, path, dst_host):
        if sw == path[-1]:
            return nc.HOSTS[dst_host]["port"]
        return self.ports[(sw, path[path.index(sw) + 1])]

    def _flow_match(self, flow):
        src, dst, sport, dport = flow.key
        return of.ofp_match(dl_type=0x0800, nw_proto=17, nw_src=IPAddr(src),
                            nw_dst=IPAddr(dst), tp_src=sport, tp_dst=dport)

    def _install_flow_path(self, flow, path, modify_ingress=False):
        """Egress first, ingress last (make-before-break)."""
        for sw in reversed(path):
            con = self.connections.get(sw)
            if con is None:
                continue
            fm = of.ofp_flow_mod(priority=PRIO_FLOW, match=self._flow_match(flow),
                                 idle_timeout=FLOW_IDLE_TIMEOUT)
            if sw == path[0]:
                fm.flags = of.OFPFF_SEND_FLOW_REM
                if modify_ingress:
                    fm.command = of.OFPFC_MODIFY_STRICT     # keeps the byte counters
            fm.actions.append(of.ofp_action_output(port=self._out_port(sw, path, flow.dst_host)))
            con.send(fm)

    def _delete_flow_rule(self, flow, sw):
        con = self.connections.get(sw)
        if con is not None:
            con.send(of.ofp_flow_mod(command=of.OFPFC_DELETE_STRICT, priority=PRIO_FLOW,
                                     match=self._flow_match(flow)))

    def _handle_FlowRemoved(self, event):
        m = event.ofp.match
        key = (_ip(m.nw_src), _ip(m.nw_dst), m.tp_src, m.tp_dst)
        flow = self.flows.get(key)
        sw = nc.DPID_TO_NAME.get(event.dpid)
        if flow is None or sw != flow.path[0]:
            return
        for s in flow.path[1:]:
            self._delete_flow_rule(flow, s)
        del self.flows[key]
        log.info("Flow %s expired (idle)", flow.name)
        self._event("FLOW_EXPIRED", flow=flow, from_path=flow.path)

    # ======================================================= link failures
    def _handle_PortStatus(self, event):
        sw = nc.DPID_TO_NAME.get(event.dpid)
        link = self.port_to_link.get((sw, event.port))
        if link is None:
            return                                    # host port
        desc = event.ofp.desc
        down = bool(event.deleted or desc.state & of.OFPPS_LINK_DOWN
                    or desc.config & of.OFPPC_PORT_DOWN)
        rev = (link[1], link[0])
        now = time.time()
        if down and link not in self.down_links:
            self.down_links.update([link, rev])
            affected = [f for f in self.flows.values()
                        if link in f.links() or rev in f.links()]
            log.warning("LINK DOWN %s-%s (%d flows affected)", link[0], link[1], len(affected))
            self._event("LINK_DOWN", link=link, detail="flows=%d" % len(affected))
            if self.mode == "static":
                self._event("NO_ACTION", link=link, detail="static routing - flows stay on "
                                                           "the failed path")
                return
            for s, con in self.connections.items():
                self._install_default_routes(s, con)
            self._recover_from_failure(link, affected, now)
        elif not down and link in self.down_links:
            self.down_links.difference_update([link, rev])
            log.info("LINK UP %s-%s", link[0], link[1])
            self._event("LINK_UP", link=link)
            if self.mode != "static":
                for s, con in self.connections.items():
                    self._install_default_routes(s, con)

    def _recover_from_failure(self, link, affected, now):
        limit = self.threshold * self.capacity
        for flow in sorted(affected, key=lambda f: -self._rate(f)):
            paths = [p for p in nc.all_paths(flow.path[0], flow.path[-1]) if self._usable(p)]
            target = choose_path(paths, flow.path, self._rate(flow),
                                 lambda p, f=flow: self._path_load(p, exclude=f), limit, None)
            if target is None:
                log.warning("No working path left for %s", flow.name)
                self._event("NO_ALTERNATE_PATH", link=link, flow=flow)
                continue
            self._reroute(flow, target, link, None, now, reason="link-failure")

    # ============================================================ monitoring
    def _tick(self):
        self.tick_no += 1
        self._evaluate(time.time())
        edge = {h["switch"] for h in nc.HOSTS.values()}
        for sw, con in list(self.connections.items()):
            con.send(of.ofp_stats_request(body=of.ofp_port_stats_request()))
            if sw in edge:
                con.send(of.ofp_stats_request(body=of.ofp_flow_stats_request()))
        if self.tick_no % 5 == 0:
            self._log_status()

    def _handle_PortStatsReceived(self, event):
        sw = nc.DPID_TO_NAME.get(event.dpid)
        now = time.time()
        for st in event.stats:
            link = self.port_to_link.get((sw, st.port_no))
            if link is None:
                continue
            if st.tx_bytes >= 0xFFFFFFFFFFFFFFFF - 1:     # "unknown" (port being deleted)
                continue
            prev = self.port_prev.get((sw, st.port_no))
            self.port_prev[(sw, st.port_no)] = (st.tx_bytes, now)
            if prev is None or now <= prev[1] or st.tx_bytes < prev[0]:
                continue
            mbps = (st.tx_bytes - prev[0]) * 8.0 / (now - prev[1]) / 1e6
            if mbps > 100 * self.capacity:                  # counter glitch, not real traffic
                continue
            # a link cannot carry more than its capacity: a higher value only means two
            # statistics replies arrived close together (e.g. right after a link failure)
            mbps = min(mbps, self.capacity)
            self.link_rate[link] = mbps
            self._write(self._linkcsv, ["%.3f" % now, "%s->%s" % link,
                                        "%.3f" % mbps, "%.3f" % (mbps / self.capacity)])

    def _handle_FlowStatsReceived(self, event):
        sw = nc.DPID_TO_NAME.get(event.dpid)
        now = time.time()
        for st in event.stats:
            if st.priority != PRIO_FLOW:
                continue
            m = st.match
            flow = self.flows.get((_ip(m.nw_src), _ip(m.nw_dst), m.tp_src, m.tp_dst))
            if flow is None or flow.path[0] != sw:
                continue
            # The time base is the SWITCH's own timer for the rule (duration_sec/nsec), not
            # the moment this reply is processed: on a busy machine replies can be handled
            # late or two at once, which would make a 2 Mbps flow look like 6 Mbps.
            dur = st.duration_sec + st.duration_nsec / 1e9
            if flow.last_bytes is not None and st.byte_count >= flow.last_bytes \
                    and dur - flow.last_time >= 0.2:
                flow.rate_mbps = (st.byte_count - flow.last_bytes) * 8.0 / \
                    (dur - flow.last_time) / 1e6
                flow.measured = True
            elif flow.last_bytes is not None and st.byte_count >= flow.last_bytes \
                    and dur >= flow.last_time:
                continue                     # too short an interval: keep the old baseline
            flow.last_bytes, flow.last_time = st.byte_count, dur
            self._write(self._flowcsv, ["%.3f" % now, flow.name,
                                        nc.path_name(flow.path), "%.3f" % flow.rate_mbps])

    def _rate(self, flow):
        """Flow rate used for decisions. In app_aware mode a brand-new flow that
        has not been measured yet uses the rate its client asked for."""
        if not flow.measured and self.mode == "app_aware" and flow.app_demand:
            return flow.app_demand
        return flow.rate_mbps

    def _offered_on_link(self, link, exclude=None):
        return sum(self._rate(f) for f in self.flows.values()
                   if f is not exclude and link in f.links())

    def _link_load(self, link, exclude=None):
        """max(measured port rate, sum of rates of the flows routed over the link)."""
        return max(self.link_rate.get(link, 0.0), self._offered_on_link(link, exclude))

    def _path_load(self, path, exclude=None):
        return max([self._link_load(l, exclude) for l in nc.path_links(path)] or [0.0])

    # ===================================================== congestion logic
    def _evaluate(self, now):
        limit = self.threshold * self.capacity
        for link in sorted(self.link_rate):
            if link in self.down_links:
                continue
            load = self._link_load(link)
            if load < limit:
                self.over_count[link] = 0
                if link in self.congested and load < self.clear_threshold * self.capacity:
                    self.congested.discard(link)
                    self.no_path_warned.discard(link)
                    log.info("Congestion cleared on %s->%s (%.2f Mbps)", link[0], link[1], load)
                    self._event("CONGESTION_CLEARED", link=link, load=load)
                continue
            if now < self.link_cooldown.get(link, 0.0):
                continue
            self.over_count[link] = self.over_count.get(link, 0) + 1
            if self.over_count[link] < self.consecutive:
                continue
            if link not in self.congested:
                self.congested.add(link)
                flows = [f.name for f in self.flows.values() if link in f.links()]
                log.warning("CONGESTION on %s->%s: %.2f Mbps (%.0f%%) flows=%s",
                            link[0], link[1], load, 100 * load / self.capacity, flows)
                self._event("CONGESTION_DETECTED", link=link, load=load,
                            detail="source=port-stats flows=" + ";".join(flows))
                if self.mode == "static":
                    self._event("NO_ACTION", link=link, load=load,
                                detail="static routing - path stays the same")
            if self.mode != "static":
                self._relieve(link, load, now)

    def _relieve(self, link, load, now):
        """Move flows off the congested link until its projected load is under the
        threshold. Usually one flow is enough; when several flows arrived at once
        (many flows), several are moved in the same decision.

        The measured port rate can never exceed the link capacity, so after the
        first move the remaining load is estimated from the per-flow rates
        (flow statistics at the ingress switch), which do show the full demand."""
        limit = self.threshold * self.capacity
        moved_rate, moved, touched = 0.0, 0, set()
        while True:
            projected = load if not moved else max(self._offered_on_link(link),
                                                   load - moved_rate)
            if moved and projected < limit:
                break
            movable = [(self._rate(f), f.created, f) for f in self.flows.values()
                       if link in f.links() and self._rate(f) > 0.05
                       and now - f.last_moved >= self.hold]
            done = False
            for flow in order_flows_to_move(movable, projected - limit):
                paths = [p for p in nc.all_paths(flow.path[0], flow.path[-1]) if self._usable(p)]
                target = choose_path(paths, flow.path, self._rate(flow),
                                     lambda p, f=flow: self._path_load(p, exclude=f),
                                     limit, projected)
                if target is not None:
                    touched.update(flow.links())
                    self._reroute(flow, target, link, projected, now, reason="congestion")
                    touched.update(flow.links())
                    moved_rate += self._rate(flow)
                    moved += 1
                    done = True
                    break
            if not done:
                break
        if moved:
            # counters of every link on the old and new paths are stale for a poll
            for l in touched:
                self.link_cooldown[l] = now + self.cooldown
                self.over_count[l] = 0
            return
        if movable and link not in self.no_path_warned:      # once per congestion episode
            self.no_path_warned.add(link)
            log.warning("No better path available for congested link %s->%s "
                        "(all paths near the threshold)", *link)
            self._event("NO_ALTERNATE_PATH", link=link, load=load)
        self.link_cooldown[link] = now + self.cooldown

    def _reroute(self, flow, new_path, link, load, now, reason):
        old_path = flow.path
        self._install_flow_path(flow, new_path, modify_ingress=True)
        for sw in old_path:
            if sw not in new_path:
                self._delete_flow_rule(flow, sw)
        flow.path = list(new_path)
        flow.last_moved = now
        flow.moves += 1
        rate = self._rate(flow)
        log.warning("REROUTE %s (%.2f Mbps): %s -> %s  [%s on %s->%s]", flow.name, rate,
                    nc.path_name(old_path), nc.path_name(new_path), reason, link[0], link[1])
        self._event("REROUTED", link=link, flow=flow, from_path=old_path, to_path=new_path,
                    load=load, detail="reason=%s flow_rate=%.2f new_path_load=%.2f"
                    % (reason, rate, self._path_load(new_path, exclude=flow)))

    def _log_status(self):
        parts = []
        for path in nc.all_paths("s1", "s5"):
            first = (path[0], path[1])
            state = " DOWN" if first in self.down_links else ""
            parts.append("%s %.1f Mbps%s" % (nc.path_name(path).split()[0],
                                              self.link_rate.get(first, 0.0), state))
        log.info("Load s1->core: %s | flows tracked: %d", " | ".join(parts), len(self.flows))


def _bool(v):
    return str(v).lower() not in ("0", "false", "no", "off")


def launch(mode="app_aware", threshold=0.8, interval=1.0, consecutive=2, cooldown=3.0,
           hold=10.0, delay_alert=30.0, loss_alert=1.0, feedback=None, log_dir=None):
    """POX entry point, e.g.:  congestion_controller --mode=static --log_dir=/tmp/x"""
    mode = str(mode).lower()
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    if log_dir is None:
        log_dir = os.path.join(PROJECT_ROOT, "results", mode)
    if feedback is None:
        feedback = nc.FEEDBACK_SOCKET
    core.registerNew(CongestionController, mode, float(threshold), float(interval),
                     int(consecutive), float(cooldown), float(hold), float(delay_alert),
                     float(loss_alert), str(feedback), str(log_dir))
