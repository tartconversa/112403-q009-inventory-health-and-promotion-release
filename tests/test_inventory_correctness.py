"""库存口径：预占、调拨、重复上报与缺报。"""

import unittest
from datetime import date

from inventory_app import InventoryEngine, Policy, Snapshot
from inventory_app.inputs import read_inventory


def inventory_row(**overrides):
    row = {
        "location": "WH1", "sku": "S1", "batch": "B1", "qty": 100.0,
        "expiry": None, "day": date(2026, 10, 1), "status": "saleable",
        "received": None, "substitute_for": None, "note": None, "_line": 2,
    }
    row.update(overrides)
    return row


def commitment_row(**overrides):
    row = {
        "id": "C1", "sku": "S1", "from": "WH1", "to": "",
        "qty": 10.0, "kind": "reservation", "state": "open",
        "day": date(2026, 10, 1), "slot": None, "budget": None,
        "min_stock": None, "note": None, "_line": 2,
    }
    row.update(overrides)
    return row


def snapshot(inventory=(), commitments=(), sales=(), columns=None):
    return Snapshot(
        inventory=list(inventory),
        commitments=list(commitments),
        sales=list(sales),
        inventory_columns=frozenset(columns if columns is not None else {"status"}),
    )


AS_OF = date(2026, 10, 1)


class ReservationTests(unittest.TestCase):
    def review(self, *commitments):
        engine = InventoryEngine(Policy(as_of=AS_OF))
        return engine.review(snapshot([inventory_row()], commitments))

    def test_open_reservation_is_deducted_not_counted_as_available(self):
        review = self.review(commitment_row(qty=40.0, state="open"))
        report = review.reports[0]
        self.assertEqual(report.sellable.low, 60.0)
        self.assertEqual(report.sellable.high, 60.0)

    def test_missing_reservation_state_gives_range(self):
        review = self.review(commitment_row(qty=40.0, state=None))
        report = review.reports[0]
        self.assertEqual((report.sellable.low, report.sellable.high), (60.0, 100.0))

    def test_released_reservation_does_not_reduce_stock(self):
        review = self.review(commitment_row(qty=40.0, state="cancelled"))
        self.assertEqual(review.reports[0].sellable.low, 100.0)

    def test_reservation_on_unknown_channel_is_flagged_not_guessed(self):
        review = self.review(commitment_row(qty=40.0, **{"from": "EC9"}))
        self.assertEqual(review.reports[0].sellable.low, 100.0)
        codes = {issue.code for issue in review.issues}
        self.assertIn("reservation_without_inventory", codes)


class TransferTests(unittest.TestCase):
    def review(self, *commitments, inventory=()):
        engine = InventoryEngine(Policy(as_of=AS_OF))
        return engine.review(snapshot(list(inventory), commitments))

    def test_in_transit_is_not_immediately_available_at_destination(self):
        inventory = [
            inventory_row(location="WH1", qty=100.0),
            inventory_row(location="ST1", batch="B2", qty=10.0),
        ]
        review = self.review(
            commitment_row(
                id="T1", kind="transfer", qty=30.0,
                state="in-transit", to="ST1",
            ),
            inventory=inventory,
        )
        st1 = next(r for r in review.reports if r.location == "ST1")
        # 接收方 10 件，若报表已提前计入则在途 30 需扣回，否则可能未计入。
        self.assertEqual(st1.sellable.low, 0.0)
        self.assertEqual(st1.sellable.high, 10.0)
        self.assertNotIn(st1.sellable.low, (10.0, 40.0))

    def test_in_transit_can_be_double_counted_in_neither_side(self):
        inventory = [
            inventory_row(location="WH1", qty=100.0),
            inventory_row(location="ST1", batch="B2", qty=10.0),
        ]
        review = self.review(
            commitment_row(
                id="T1", kind="transfer", qty=30.0,
                state="in-transit", to="ST1",
            ),
            inventory=inventory,
        )
        wh1 = next(r for r in review.reports if r.location == "WH1")
        st1 = next(r for r in review.reports if r.location == "ST1")
        # 两侧合计低端不超过 100（发方 70）+ 0（收方）= 70；
        # 高端不超过 100（发方未扣）+ 10（收方）= 110，不含额外的 30。
        self.assertEqual(wh1.sellable.low, 70.0)
        self.assertLessEqual(wh1.sellable.high + st1.sellable.high, 110.0)

    def test_received_transfer_does_not_double_deduct(self):
        inventory = [
            inventory_row(location="WH1", qty=100.0),
            inventory_row(location="ST1", batch="B2", qty=40.0),
        ]
        review = self.review(
            commitment_row(
                id="T1", kind="transfer", qty=30.0,
                state="received", to="ST1",
            ),
            inventory=inventory,
        )
        wh1 = next(r for r in review.reports if r.location == "WH1")
        st1 = next(r for r in review.reports if r.location == "ST1")
        self.assertEqual(wh1.sellable.low, 100.0)
        self.assertEqual(st1.sellable.low, 40.0)


class DuplicateSnapshotTests(unittest.TestCase):
    def review(self, rows):
        return InventoryEngine(Policy(as_of=AS_OF)).review(snapshot(rows))

    def test_same_batch_two_sources_are_not_summed(self):
        rows = [
            inventory_row(qty=100.0, _line=2),
            inventory_row(qty=60.0, _line=3),
        ]
        review = self.review(rows)
        self.assertEqual(review.reports[0].gross.low, 60.0)
        self.assertEqual(review.reports[0].gross.high, 100.0)
        codes = {issue.code for issue in review.issues}
        self.assertIn("conflicting_snapshot_qty", codes)

    def test_stale_snapshot_does_not_add_stock(self):
        rows = [
            inventory_row(qty=100.0, day=date(2026, 9, 1), _line=2),
            inventory_row(qty=40.0, day=date(2026, 10, 1), _line=3),
        ]
        review = self.review(rows)
        self.assertEqual(review.reports[0].gross.low, 40.0)
        self.assertEqual(review.reports[0].gross.high, 40.0)
        codes = {issue.code for issue in review.issues}
        self.assertIn("stale_snapshot_ignored", codes)

    def test_missing_qty_is_unbounded_not_zero(self):
        rows = [inventory_row(qty=None)]
        review = self.review(rows)
        self.assertEqual(review.reports[0].sellable.low, 0.0)
        self.assertIsNone(review.reports[0].sellable.high)

    def test_blocked_status_reduces_saleable(self):
        rows = [inventory_row(qty=50.0, status="blocked")]
        review = self.review(rows)
        self.assertEqual(review.reports[0].sellable.low, 0.0)
        self.assertEqual(review.reports[0].sellable.high, 0.0)
        self.assertEqual(review.reports[0].blocked.low, 50.0)

    def test_conflicting_status_takes_conservative_end(self):
        rows = [
            inventory_row(qty=50.0, status="saleable", _line=2),
            inventory_row(qty=50.0, status="blocked", _line=3),
        ]
        review = self.review(rows)
        self.assertEqual(review.reports[0].sellable.low, 0.0)
        self.assertEqual(review.reports[0].sellable.high, 0.0)
        self.assertIn("conflicting_inventory_status", {i.code for i in review.issues})

    def test_status_column_absent_is_assumption_not_double_count(self):
        # 样例文件没有 status 列；数量本身仍然精确。
        review = InventoryEngine(Policy(as_of=AS_OF)).review(
            snapshot([inventory_row(qty=50.0, status=None)], columns={"qty", "expiry"})
        )
        self.assertEqual(review.reports[0].sellable.low, 50.0)
        self.assertIn("inventory_status_assumed", {i.code for i in review.issues})

    def test_status_blank_with_column_present_is_unconfirmed(self):
        review = InventoryEngine(Policy(as_of=AS_OF)).review(
            snapshot([inventory_row(qty=50.0, status=None)], columns={"status"})
        )
        self.assertEqual(review.reports[0].sellable.low, 0.0)
        self.assertEqual(review.reports[0].sellable.high, 50.0)


class InputReadingTests(unittest.TestCase):
    def test_sample_inventory_columns_are_detected(self):
        from pathlib import Path

        snap = read_inventory(Path(__file__).resolve().parent.parent / "samples" / "inventory.csv")
        self.assertNotIn("status", snap.inventory_columns)
        self.assertEqual(len(snap.inventory), 3)


if __name__ == "__main__":
    unittest.main()
