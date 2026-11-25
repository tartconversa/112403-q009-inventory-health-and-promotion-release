"""数量范围的基本性质。"""

import unittest

from inventory_app.model import Range


class RangeArithmetic(unittest.TestCase):
    def test_addition_keeps_unknown_upper_bound(self):
        total = Range(3, 5) + Range(2, None)
        self.assertEqual(total.low, 5)
        self.assertIsNone(total.high)
        self.assertTrue(total.is_unbounded)

    def test_subtraction_is_conservative_at_both_ends(self):
        # 扣减区间：低位按对方最大值，高位按对方最小值。
        net = Range(10, 20) - Range(4, 7)
        self.assertEqual((net.low, net.high), (3, 16))

    def test_subtraction_never_negative(self):
        net = Range(2, 3) - Range(0, 10)
        self.assertEqual((net.low, net.high), (0, 3))

    def test_subtraction_with_unknown_subtrahend(self):
        net = Range(10, 12) - Range(0, None)
        self.assertEqual(net.low, 0)
        self.assertEqual(net.high, 12)

    def test_range_ordering_is_repaired(self):
        repaired = Range(8, 3)
        self.assertEqual((repaired.low, repaired.high), (8, 8))

    def test_negative_low_is_clamped(self):
        self.assertEqual(Range(-5, 4).low, 0)


if __name__ == "__main__":
    unittest.main()
