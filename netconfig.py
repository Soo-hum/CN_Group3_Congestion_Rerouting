"""
netconfig.py - Single source of truth for the network used in this project.

Both the Mininet side (topology.py, experiment.py) and the SDN controller
(controller/congestion_controller.py) import this file, so the controller
always knows exactly which switch port leads where.

Topology (3 equal-cost paths between the two edge switches):

                 +------ s2 ------+          P1 = s1 -> s2 -> s5  (default path)
                 |                |
   h1 --+        |                |        +-- h3
        +--- s1 -+------ s3 ------+- s5 ---+     P2 = s1 -> s3 -> s5
   h2 --+        |                |        +-- h4
                 |                |              P3 = s1 -> s4 -> s5
                 +------ s4 ------+

Every switch-to-switch link is limited to CORE_LINK_BW_MBPS so that
congestion can be created in a controlled way. Host links are not limited
(HOST_LINK_BW_MBPS = None) so that they never become the bottleneck.
"""

# --------------------------------------------------------------------------
# Link capacities (Mbps)
# --------------------------------------------------------------------------
CORE_LINK_BW_MBPS = 10      # s1<->s2/s3/s4 and s2/s3/s4<->s5
HOST_LINK_BW_MBPS = None    # host <-> edge switch: not shaped (None). A tc/HTB limit
                            # of 100 Mbps only makes Linux print "quantum ... is big"

# Router buffer on core links, in packets. None = Linux default (txqueuelen,
# 1000 packets ~ 1 s of queueing at 10 Mbps). Set e.g. 100 for a smaller,
# more realistic buffer (uses the sch_netem kernel module, standard on Ubuntu).
CORE_LINK_MAX_QUEUE = None

# --------------------------------------------------------------------------
# Switches: name -> OpenFlow datapath id (dpid)
# --------------------------------------------------------------------------
SWITCHES = {
    "s1": 1,   # left edge switch (senders attach here)
    "s2": 2,   # core switch on path P1
    "s3": 3,   # core switch on path P2
    "s4": 4,   # core switch on path P3
    "s5": 5,   # right edge switch (receivers attach here)
}
DPID_TO_NAME = {v: k for k, v in SWITCHES.items()}

# --------------------------------------------------------------------------
# Hosts: fixed IP + MAC, and the (switch, port) they plug into
# --------------------------------------------------------------------------
HOSTS = {
    "h1": {"ip": "10.0.0.1", "mac": "00:00:00:00:00:01", "switch": "s1", "port": 1},
    "h2": {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02", "switch": "s1", "port": 2},
    "h3": {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03", "switch": "s5", "port": 1},
    "h4": {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04", "switch": "s5", "port": 2},
}
IP_TO_HOST = {v["ip"]: k for k, v in HOSTS.items()}

# --------------------------------------------------------------------------
# Switch-to-switch links: (switchA, portA, switchB, portB)
# Port numbers are fixed explicitly in Mininet so the controller can rely on them.
# --------------------------------------------------------------------------
SWITCH_LINKS = [
    ("s1", 3, "s2", 1),
    ("s1", 4, "s3", 1),
    ("s1", 5, "s4", 1),
    ("s2", 2, "s5", 3),
    ("s3", 2, "s5", 4),
    ("s4", 2, "s5", 5),
]

# Human-friendly names for the three paths (used in logs and plots)
PATH_NAMES = {
    ("s1", "s2", "s5"): "P1 (via s2)",
    ("s1", "s3", "s5"): "P2 (via s3)",
    ("s1", "s4", "s5"): "P3 (via s4)",
}

# --------------------------------------------------------------------------
# Controller connection (POX listens here; Mininet connects here)
# --------------------------------------------------------------------------
CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653

# Application -> controller feedback channel (Socket-SDN integration).
# The UDP server on a Mininet host sends JSON QoS reports to this UNIX
# datagram socket; the controller listens on it. Mininet hosts share the
# file system with the controller, so the path is reachable from every host.
FEEDBACK_SOCKET = "/tmp/sdn_app_feedback.sock"


# --------------------------------------------------------------------------
# Helper functions shared by controller and analysis code
# --------------------------------------------------------------------------
def port_map():
    """Return {(swA, swB): port on swA that leads to swB} for every switch link."""
    ports = {}
    for a, pa, b, pb in SWITCH_LINKS:
        ports[(a, b)] = pa
        ports[(b, a)] = pb
    return ports


def neighbours():
    """Return adjacency list {switch: [neighbour switches]} (sorted, deterministic)."""
    adj = {s: [] for s in SWITCHES}
    for a, _, b, _ in SWITCH_LINKS:
        adj[a].append(b)
        adj[b].append(a)
    for s in adj:
        adj[s].sort(key=lambda n: SWITCHES[n])
    return adj


def all_paths(src_sw, dst_sw):
    """All loop-free switch paths from src_sw to dst_sw.

    Sorted by (hop count, dpids) so index 0 is the deterministic
    'default' shortest path that a static routing scheme would use.
    """
    if src_sw == dst_sw:
        return [[src_sw]]
    adj = neighbours()
    paths, stack = [], [(src_sw, [src_sw])]
    while stack:
        node, path = stack.pop()
        for nxt in adj[node]:
            if nxt in path:
                continue
            if nxt == dst_sw:
                paths.append(path + [nxt])
            else:
                stack.append((nxt, path + [nxt]))
    paths.sort(key=lambda p: (len(p), [SWITCHES[s] for s in p]))
    return paths


def path_links(path):
    """Directed links [(a,b), ...] that make up a switch path."""
    return [(path[i], path[i + 1]) for i in range(len(path) - 1)]


def path_name(path):
    """Readable name of a path, e.g. 'P2 (via s3)'."""
    key = tuple(path)
    if key in PATH_NAMES:
        return PATH_NAMES[key]
    rev = tuple(reversed(path))
    if rev in PATH_NAMES:
        return PATH_NAMES[rev] + " [reverse]"
    return "->".join(path)
