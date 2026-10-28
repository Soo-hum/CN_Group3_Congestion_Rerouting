"""
routing_logic.py - The two routing decisions of the controller, written as
plain functions (no POX imports) so they can be unit-tested on any machine.

    order_flows_to_move()  which flow should leave a congested link?
    choose_path()          which path should it move to?
"""


def order_flows_to_move(candidates, excess):
    """candidates: list of (rate_mbps, created_time, item). Returns items in the
    order they should be tried.

    1st choice: the SMALLEST flow that alone removes the excess load (least
                disturbance). Rates are compared in 1 Mbps steps and the NEWEST
                flow wins a tie, because it is the one that caused the congestion.
    otherwise : the BIGGEST flows first, to relieve as much as possible.
    """
    enough = sorted([c for c in candidates if c[0] >= excess],
                    key=lambda c: (round(c[0]), -c[1]))
    rest = sorted([c for c in candidates if c[0] < excess],
                  key=lambda c: (-c[0], -c[1]))
    return [c[2] for c in enough + rest]


def choose_path(paths, current, flow_rate, path_load, limit, congested_load=None):
    """Pick the least-loaded alternative path for a flow.

    paths          candidate paths (lists of switch names), already without failed links
    current        the flow's current path (never returned)
    flow_rate      Mbps the flow needs
    path_load      function(path) -> Mbps on the busiest link of that path
    limit          congestion limit in Mbps (threshold x capacity)
    congested_load load of the congested link (None = link failure: any path is better)

    Paths that can take the flow while staying under `limit` are preferred; among
    them the least loaded wins (then fewer hops, then the order of `paths`). If no
    path has enough head-room, a path is only accepted when it still lowers the
    worst load, i.e. load + flow_rate < congested_load.
    """
    best = None
    for idx, path in enumerate(paths):
        if path == current:
            continue
        load = path_load(path)
        projected = load + flow_rate
        rank = (projected > limit, load, len(path), idx)
        if best is None or rank < best[0]:
            best = (rank, path, projected)
    if best is None:
        return None
    _, path, projected = best
    if congested_load is not None and projected > limit and projected >= congested_load:
        return None
    return path
