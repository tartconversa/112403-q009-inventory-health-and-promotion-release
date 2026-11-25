"""事件修订：盘点差异、退货与调拨签收如何改变风险与推荐分配。"""

import unittest
from datetime import date

from inventory_app import InventoryEngine, Policy, Snapshot
from inventory_app.events import apply_events

AS_OF = date(2026, 10, 1)


def inventory_row(location, qty, *, sku="S1", batch="B1", day=AS_OF, status="saleable"):
    return {
        "location": location, "sku": sku, "batch": batch, "qty": float(qty),
        "expiry": date(2027, 6, 1), "day": day, "status": status,
        "received": None, "substitute_for": None, "note": None, "_line": 2,
    }


def commitment(ident, *, sku="S1", frm="", to="", qty=0.0, kind="marketing",
               state="proposed", day=AS_OF):
    return {
        "id": ident, "sku": sku, "from": frm, "to": to, "qty": float(qty),
        "kind": kind, "state": state, "day": day, "slot": None,
        "budget": None, "min_stock": None, "note": None, "_line": 2,
    }


def sale(location, units, sku="S1", returns=0.0):
    return {
        "sku": sku, "location": location, "units": float(units),
        "returns": float(returns), "spend": 0.0, "period": "2026-09", "_line": 2,
    }


def snapshot(inventory, commitments=(), sales=()):
    return Snapshot(
        inventory=list(inventory), commitments=list(commitments),
        sales=list(sales), inventory_columns=frozenset({"status"}),
    )


def engine():
    return InventoryEngine(Policy(as_of=AS_OF))


class CountRevisionTests(unittest.TestCase):
    def test_count_revises_batch_and_updates_risk(self):
        snap = snapshot(
            [inventory_row("ST1", 5)],
            [commitment("M1", frm="ST1", qty=25, state="confirmed")],
            [sale("ST1", 150)],
        )
        revision = apply_events(snap, [{
            "id": "E1", "kind": "count", "sku": "S1", "location": "ST1",
            "batch": "B1", "qty": 60.0, "day": date(2026, 10, 2),
        }], engine())
        before = revision.before.reports[0]
        after = revision.after.reports[0]
        self.assertLess(before.sellable.low, after.sellable.low)
        self.assertEqual(after.sellable.low, 60.0)
        self.assertTrue(revision.changes)
        self.assertEqual(revision.changes[0].before_risk, "shortage")

    def test_count_does_not_overwrite_history(self):
        snap = snapshot([inventory_row("ST1", 5, day=date(2026, 9, 1))], sales=[sale("ST1", 150)])
        revision = apply_events(snap, [{
            "id": "E1", "kind": "count", "sku": "S1", "location": "ST1",
            "batch": "B1", "qty": 40.0, "day": date(2026, 10, 2),
        }], engine())
        # 旧快照保留在原文件中，修订写入新版本，因此重算仍取最新日期的 40。
        self.assertEqual(revision.after.reports[0].sellable.low, 40.0)
        self.assertEqual(len(revision.before.reports), 1)

    def test_count_for_unknown_batch_is_added_with_warning(self):
        snap = snapshot([inventory_row("ST1", 5)], sales=[sale("ST1", 30)])
        revision = apply_events(snap, [{
            "id": "E1", "kind": "count", "sku": "S1", "location": "ST1",
            "batch": "B9", "qty": 12.0, "day": date(2026, 10, 2),
        }], engine())
        codes = {issue.code for issue in revision.issues}
        self.assertIn("count_without_snapshot", codes)


class ReturnRevisionTests(unittest.TestCase):
    def test_return_adds_stock_and_lowers_net_demand(self):
        snap = snapshot([inventory_row("ST1", 5)], sales=[sale("ST1", 30, returns=2)])
        revision = apply_events(snap, [{
            "id": "E2", "kind": "return", "sku": "S1", "location": "ST1",
            "qty": 6.0, "day": date(2026, 10, 2),
        }], engine())
        after = revision.after.reports[0]
        self.assertEqual(after.sellable.low, 11.0)
        self.assertLess(after.daily_high, revision.before.reports[0].daily_high)

    def test_return_without_location_is_rejected(self):
        snap = snapshot([inventory_row("ST1", 5)], sales=[sale("ST1", 30)])
        revision = apply_events(snap, [{
            "id": "E2", "kind": "return", "sku": "S1", "qty": 6.0,
            "day": date(2026, 10, 2),
        }], engine())
        self.assertIn("return_incomplete", {issue.code for issue in revision.issues})


class ReceiptRevisionTests(unittest.TestCase):
    def test_receipt_resolves_in_transit_and_adds_stock(self):
        snap = snapshot(
            [inventory_row("WH1", 100), inventory_row("ST1", 10, batch="B2")],
            [commitment("T1", kind="transfer", frm="WH1", to="ST1", qty=30, state="in-transit")],
            [sale("ST1", 30), sale("WH1", 10)],
        )
        revision = apply_events(snap, [{
            "id": "E3", "kind": "receipt", "sku": "S1", "location": "ST1",
            "qty": 30.0, "target": "T1", "day": date(2026, 10, 2),
        }], engine())
        after = {report.location: report for report in revision.after.reports}
        self.assertEqual(after["ST1"].sellable.low, 40.0)
        self.assertIn("receipt_recorded", {issue.code for issue in revision.issues})
        # 在途重复计入风险解除后，发出方不再被按未扣减处理。
        self.assertEqual(after["WH1"].sellable.low, 100.0)

    def test_receipt_without_quantity_only_updates_state(self):
        snap = snapshot(
            [inventory_row("WH1", 100), inventory_row("ST1", 10, batch="B2")],
            [commitment("T1", kind="transfer", frm="WH1", to="ST1", qty=30, state="unknown")],
            [sale("ST1", 30), sale("WH1", 10)],
        )
        # 状态未知时两侧都按可能重复计入扣减；签收后只解除重复计入风险，
        # 数量保持接收方报表已有的 10 件。
        revision = apply_events(snap, [{
            "id": "E3", "kind": "receipt", "sku": "S1", "location": "ST1",
            "target": "T1", "day": date(2026, 10, 2),
        }], engine())
        before = {report.location: report for report in revision.before.reports}
        after = {report.location: report for report in revision.after.reports}
        self.assertEqual(before["ST1"].sellable.low, 0.0)
        self.assertEqual(after["ST1"].sellable.low, 10.0)
        self.assertIn("receipt_missing_qty", {issue.code for issue in revision.issues})


class ProtectedDecisionTests(unittest.TestCase):
    def test_confirmed_decision_is_preserved_across_revision(self):
        snap = snapshot(
            [inventory_row("ST1", 100)],
            [commitment("M1", frm="ST1", qty=20, state="confirmed")],
            [sale("ST1", 30)],
        )
        revision = apply_events(snap, [{
            "id": "E1", "kind": "count", "sku": "S1", "location": "ST1",
            "batch": "B1", "qty": 2.0, "day": date(2026, 10, 2),
        }], engine())
        after_allocation = next(
            a for a in revision.after.plan.allocations if a.resource_id == "M1"
        )
        self.assertEqual(after_allocation.approved, 20)
        self.assertTrue(after_allocation.protected)
        self.assertEqual(after_allocation.status, "manual")
        self.assertIn("M1", {item["id"] for item in revision.after.manual_items})

    def test_unconfirmed_reallocation_is_reported(self):
        snap = snapshot(
            [inventory_row("ST1", 100)],
            [commitment("M2", frm="ST1", qty=80, state="proposed")],
            [sale("ST1", 30)],
        )
        revision = apply_events(snap, [{
            "id": "E1", "kind": "count", "sku": "S1", "location": "ST1",
            "batch": "B1", "qty": 20.0, "day": date(2026, 10, 2),
        }], engine())
        changes = {item["id"]: item for item in revision.allocation_changes}
        self.assertIn("M2", changes)
        self.assertEqual(changes["M2"]["change"], "reallocated")


if __name__ == "__main__":
    unittest.main()
