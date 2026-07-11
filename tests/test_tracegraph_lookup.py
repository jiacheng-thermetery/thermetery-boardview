import unittest

from src.tvw_topology import TraceGraph


class _CountingNames(list):
    def __init__(self, values):
        super().__init__(values)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


class TraceGraphLookupTests(unittest.TestCase):
    def test_reverse_net_index_is_built_once_and_keeps_first_duplicate(self):
        names = _CountingNames(["", "GND", "VCC", "GND"])
        graph = TraceGraph(net_names=names)

        self.assertEqual(graph.net_id_by_name("GND"), 1)
        self.assertEqual(graph.net_id_by_name("VCC"), 2)
        self.assertIsNone(graph.net_id_by_name("MISSING"))
        self.assertEqual(names.iterations, 1)


if __name__ == "__main__":
    unittest.main()
