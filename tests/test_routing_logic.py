"""Unit tests for the controller's routing decisions and the topology description.

Run:  python3 -m unittest discover -s tests -v      (no Mininet / POX needed)
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "controller"))
import netconfig as nc  # noqa: E402
from routing_logic import order_flows_to_move, choose_path  # noqa: E402

P1, P2, P3 = ["s1", "s2", "s5"], ["s1", "s3", "s5"], ["s1", "s4", "s5"]


class FlowSelection(unittest.TestCase):

    def test_smallest_flow_that_removes_the_excess(self):
        # excess 2 Mbps: the 3 Mbps flow is enough and disturbs less than the 6 Mbps one
        order = order_flows_to_move([(6.0, 1, "big"), (3.0, 2, "small"), (1.0, 3, "tiny")], 2.0)
        self.assertEqual(order[0], "small")

    def test_tie_goes_to_newest_flow(self):
        order = order_flows_to_move([(6.2, 10, "old"), (6.1, 20, "new")], 4.0)
        self.assertEqual(order[0], "new")

    def test_no_single_flow_is_enough_biggest_first(self):
        order = order_flows_to_move([(1.0, 1, "a"), (3.0, 2, "b"), (2.0, 3, "c")], 9.0)
        self.assertEqual(order, ["b", "c", "a"])


class PathSelection(unittest.TestCase):
    LIMIT = 8.0   # 80 % of 10 Mbps

    def test_least_loaded_path(self):
        loads = {tuple(P2): 6.2, tuple(P3): 0.0}
        p = choose_path([P1, P2, P3], P1, 6.0, lambda x: loads.get(tuple(x), 0), self.LIMIT, 12.4)
        self.assertEqual(p, P3)

    def test_never_returns_current_path(self):
        p = choose_path([P1], P1, 6.0, lambda x: 0.0, self.LIMIT, 12.0)
        self.assertIsNone(p)

    def test_prefers_path_with_headroom_over_fewer_hops(self):
        long_path = ["s1", "s3", "s5", "s4"]
        loads = {tuple(P2): 7.0, tuple(long_path): 0.0}
        p = choose_path([P2, long_path], P1, 3.0, lambda x: loads[tuple(x)], self.LIMIT, 12.0)
        self.assertEqual(p, long_path)

    def test_best_effort_move_when_it_still_helps(self):
        loads = {tuple(P2): 6.0, tuple(P3): 7.0}
        # no path fits under 8, but 6 + 3 = 9 < 14 (current congested load) -> move
        p = choose_path([P1, P2, P3], P1, 3.0, lambda x: loads.get(tuple(x), 0), self.LIMIT, 14.0)
        self.assertEqual(p, P2)

    def test_no_move_when_it_would_not_help(self):
        loads = {tuple(P2): 9.0, tuple(P3): 9.5}
        p = choose_path([P1, P2, P3], P1, 3.0, lambda x: loads.get(tuple(x), 0), self.LIMIT, 11.0)
        self.assertIsNone(p)

    def test_link_failure_takes_any_working_path(self):
        loads = {tuple(P2): 9.0, tuple(P3): 9.5}
        p = choose_path([P2, P3], P1, 3.0, lambda x: loads[tuple(x)], self.LIMIT, None)
        self.assertEqual(p, P2)


class Topology(unittest.TestCase):

    def test_three_paths_between_edge_switches(self):
        paths = nc.all_paths("s1", "s5")
        self.assertEqual(paths[:3], [P1, P2, P3])
        self.assertTrue(all(len(p) == 3 for p in paths[:3]))

    def test_default_path_is_p1_both_ways(self):
        self.assertEqual(nc.all_paths("s1", "s5")[0], P1)
        self.assertEqual(nc.all_paths("s5", "s1")[0], ["s5", "s2", "s1"])

    def test_port_numbers_are_unique_per_switch(self):
        used = {}
        for name, h in nc.HOSTS.items():
            used.setdefault(h["switch"], []).append(h["port"])
        for a, pa, b, pb in nc.SWITCH_LINKS:
            used.setdefault(a, []).append(pa)
            used.setdefault(b, []).append(pb)
        for sw, ports in used.items():
            self.assertEqual(len(ports), len(set(ports)), "duplicate port on %s" % sw)

    def test_port_map_is_symmetric(self):
        pm = nc.port_map()
        for (a, b) in pm:
            self.assertIn((b, a), pm)

    def test_path_names(self):
        self.assertEqual(nc.path_name(P2), "P2 (via s3)")
        self.assertIn("reverse", nc.path_name(list(reversed(P3))))


if __name__ == "__main__":
    unittest.main()
