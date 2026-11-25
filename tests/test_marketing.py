"""营销资源：保护、重排、预算与渠道张力。"""

import unittest
from datetime import date

from inventory_app import InventoryEngine, Policy, Snapshot


def row(location, qty, *, sku="S1", status="saleable", expiry=date(2027, 1, 1)):
    return {
        "location": location, "sku": sku, "batch": f"B-{location}",
        "qty": float(qty), "expiry": expiry, "day": date(2026, 10, 1),
        "status": status, "received": None, "substitute_for": None,
        "note": None, "_line": 2,
    }


def commitment(ident, sku="S1", frm="", to="", qty=0.0, kind="marketing",
               state="proposed", budget=None, min_stock=None, slot=None):
    return {
        "id": ident, "sku": sku, "from": frm, "to": to, "qty": float(qty),
        "kind": kind, "state": state, "day": date(2026, 10, 1),
        "slot": slot, "budget": budget, "min_stock": min_stock,
        "note": None, "_line": 2,
    }


def sales(location, sku="S1", units=30.0, returns=0.0):
    return {
        "sku": sku, "location": location, "units": units, "returns": returns,
        "spend": 0.0, "period": "2026-09", "_line": 2,
    }


def review(inventory, commitments, sales_rows=(), **policy_kwargs):
    policy = Policy(as_of=date(2026, 10, 1), **policy_kwargs)
    snap = Snapshot(
        inventory=list(inventory), commitments=list(commitments),
        sales=list(sales_rows), inventory_columns=frozenset({"status"}),
    )
    return InventoryEngine(policy).review(snap)


class ProtectionTests(unittest.TestCase):
    def test_confirmed_resource_keeps_decision_when_stock_is_tight(self):
        result = review(
            [row("ST1", 10)],
            [commitment("M1", frm="ST1", qty=25, state="confirmed")],
            [sales("ST1", units=60.0)],
        )
        allocation = next(a for a in result.plan.allocations if a.resource_id == "M1")
        self.assertTrue(allocation.protected)
        self.assertEqual(allocation.approved, 25)
        self.assertEqual(allocation.status, "manual")
        self.assertEqual(allocation.shortfall, 15)
        self.assertIn("M1", {item["id"] for item in result.manual_items})

    def test_confirmed_resource_within_stock_is_protected_not_manual(self):
        result = review(
            [row("ST1", 100)],
            [commitment("M1", frm="ST1", qty=20, state="confirmed")],
            [sales("ST1", units=30.0)],
        )
        allocation = next(a for a in result.plan.allocations if a.resource_id == "M1")
        self.assertEqual(allocation.status, "protected")
        self.assertEqual(result.manual_items, [])

    def test_minimum_stock_commitment_is_respected(self):
        result = review(
            [row("ST1", 100)],
            [
                commitment("M1", frm="ST1", qty=20, state="confirmed"),
                commitment("M2", frm="ST1", qty=90, state="proposed"),
            ],
            [sales("ST1", units=30.0)],
            min_stock={"ST1": {"S1": 40.0}},
        )
        m2 = next(a for a in result.plan.allocations if a.resource_id == "M2")
        # 100 可售 − 40 最低库存 − 20 已确认营销 = 40
        self.assertEqual(m2.approved, 40)
        self.assertEqual(m2.shortfall, 50)

    def test_unconfirmed_can_be_reallocated_after_stock_revision(self):
        from inventory_app.events import apply_events

        snapshot = Snapshot(
            inventory=[row("ST1", 100)],
            commitments=[
                commitment("M1", frm="ST1", qty=20, state="confirmed"),
                commitment("M2", frm="ST1", qty=80, state="proposed"),
            ],
            sales=[sales("ST1", units=30.0)],
            inventory_columns=frozenset({"status"}),
        )
        engine = InventoryEngine(Policy(as_of=date(2026, 10, 1)))
        result = apply_events(snapshot, [{
            "id": "E1", "kind": "count", "sku": "S1", "location": "ST1",
            "batch": "B-ST1", "qty": 30.0, "day": date(2026, 10, 2),
        }], engine)
        before = {a.resource_id: a for a in result.before.plan.allocations}
        after = {a.resource_id: a for a in result.after.plan.allocations}
        self.assertGreater(before["M2"].approved, after["M2"].approved)
        self.assertEqual(after["M1"].approved, 20)
        self.assertTrue(after["M1"].protected)


class BudgetTests(unittest.TestCase):
    def test_budget_overrun_releases_lowest_priority_first(self):
        resources = [
            commitment("M1", frm="ST1", qty=10, state="proposed", budget=60.0),
            commitment("M2", frm="ST1", qty=10, state="proposed", budget=60.0),
        ]
        result = review(
            [row("ST1", 500)],
            resources,
            [sales("ST1", units=10.0)],
            budget_total=100.0,
        )
        self.assertIn("budget_exceeded", {i.code for i in result.issues})
        released = [a for a in result.plan.allocations if a.status == "released"]
        self.assertEqual(len(released), 1)

    def test_missing_budget_is_flagged_not_silently_ignored(self):
        result = review(
            [row("ST1", 100)],
            [commitment("M1", frm="ST1", qty=10, state="proposed")],
            [sales("ST1", units=30.0)],
        )
        self.assertTrue(result.plan.budget_unverified)
        self.assertIn("budget_missing", {i.code for i in result.issues})


class ChannelTensionTests(unittest.TestCase):
    def test_shortage_in_one_channel_and_overstock_in_another_is_surfaced(self):
        result = review(
            [row("ST1", 5), row("WH1", 500)],
            [],
            [sales("ST1", units=150.0), sales("WH1", sku="S1", units=1.0)],
        )
        portfolio = next(p for p in result.portfolio if p.sku == "S1")
        self.assertEqual(portfolio.tension, "mixed")
        self.assertTrue(portfolio.tension_detail)
        self.assertEqual(
            portfolio.tension_detail[0]["shortage_channel"], "ST1"
        )

    def test_promotion_into_shortage_channel_is_flagged(self):
        result = review(
            [row("ST1", 10), row("WH1", 500)],
            [commitment("M1", frm="ST1", qty=5, state="proposed")],
            [sales("ST1", units=150.0), sales("WH1", sku="S1", units=1.0)],
        )
        codes = {i.code for i in result.issues}
        self.assertIn("promotion_into_shortage", codes)

    def test_same_channel_healthy_has_no_tension(self):
        result = review(
            [row("ST1", 60)],
            [],
            [sales("ST1", units=30.0)],
        )
        self.assertEqual(result.portfolio[0].tension, "none")


if __name__ == "__main__":
    unittest.main()
