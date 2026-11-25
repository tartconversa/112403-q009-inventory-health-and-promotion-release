"""引擎编排：从快照到一次评审结果。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from .demand import Demand, build_demand
from .inputs import Snapshot
from .marketing import Plan, plan_marketing
from .model import Issue, Policy, Range
from .normalize import Normalized, normalize
from .reports import (
    Portfolio,
    VenueReport,
    build_inventory_reports,
    build_portfolio,
    check_snapshot_freshness,
)
from .supply import (
    VenueSupply,
    build_supply,
    merge_holdings,
    reservation_impacts,
    transfer_impacts,
)


@dataclass
class Review:
    """一次完整的库存健康与营销资源评审。"""

    as_of: date
    policy: Policy
    reports: list[VenueReport] = field(default_factory=list)
    portfolio: list[Portfolio] = field(default_factory=list)
    plan: Plan = field(default_factory=Plan)
    issues: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    venues: dict[tuple[str, str], VenueSupply] = field(default_factory=dict)
    sales_basis: dict[tuple[str, str], Demand] = field(default_factory=dict)
    holdings: int = 0
    reservations: int = 0
    transfers: int = 0
    resources: int = 0
    unrecognized_counts: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "holdings": self.holdings,
            "reservations": self.reservations,
            "transfers": self.transfers,
            "marketing_resources": self.resources,
            "issues_critical": sum(1 for i in self.issues if i.severity == "critical"),
            "issues_warning": sum(1 for i in self.issues if i.severity == "warning"),
        }

    @property
    def manual_items(self) -> list[dict[str, Any]]:
        """已对外确认但出现风险的营销安排：保留原决策，转人工。"""
        items: list[dict[str, Any]] = []
        for allocation in self.plan.allocations:
            if allocation.protected and allocation.status == "manual":
                items.append(allocation.as_dict())
        return items

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "counts": self.counts,
            "inventory": [report.as_dict() for report in self.reports],
            "portfolio": [
                {
                    "sku": portfolio.sku,
                    "total_sellable": portfolio.total_sellable.as_dict(),
                    "total_blocked": portfolio.total_blocked.as_dict(),
                    "tension": portfolio.tension,
                    "tension_detail": portfolio.tension_detail,
                }
                for portfolio in self.portfolio
            ],
            "marketing": {
                "budget_total": _round(self.plan.budget_total),
                "budget_committed": _round(self.plan.budget_committed),
                "budget_proposed": _round(self.plan.budget_proposed),
                "budget_unverified": self.plan.budget_unverified,
                "allocations": [allocation.as_dict() for allocation in self.plan.allocations],
                "tensions": [tension.as_dict() for tension in self.plan.tensions],
            },
            "manual_items": self.manual_items,
            "issues": [issue.as_dict() for issue in sorted(self.issues, key=lambda i: i.sort_key())],
            "notes": self.notes,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _as_of(snapshot: Snapshot, policy: Policy) -> date:
    if policy.as_of is not None:
        return policy.as_of
    days = [row["day"] for row in snapshot.inventory if row.get("day") is not None]
    if days:
        return max(days)
    return date.today()


def inventory_keys(
    normalized: Normalized,
    extra: Iterable[tuple[str | None, str | None]] = (),
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for holding in normalized.holdings:
        keys.add((holding.location, holding.sku))
    for reservation in normalized.reservations:
        if reservation.location:
            keys.add((reservation.location, reservation.sku))
    for transfer in normalized.transfers:
        if transfer.origin:
            keys.add((transfer.origin, transfer.sku))
        if transfer.destination:
            keys.add((transfer.destination, transfer.sku))
    for marketing in normalized.marketing:
        if marketing.channel:
            keys.add((marketing.channel, marketing.sku))
    for location, sku in extra:
        if location and sku:
            keys.add((location, sku))
    return keys


class InventoryEngine:
    """无状态引擎；每次 :meth:`review` 都基于传入快照重新计算。"""

    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy()

    def review(
        self,
        snapshot: Snapshot,
        *,
        demand_adjustments: Sequence[dict[str, Any]] = (),
        event_issues: Sequence[Issue] = (),
    ) -> Review:
        policy = self.policy
        as_of = _as_of(snapshot, policy)
        normalized = normalize(snapshot, policy)
        issues: list[Issue] = list(normalized.issues) + list(event_issues)

        supply_by_venue, supply_issues = merge_holdings(normalized.holdings)
        issues.extend(supply_issues)
        inventory_locations = {
            (holding.location, holding.sku) for holding in normalized.holdings
        }

        reservation_map, reservation_issues = reservation_impacts(
            normalized.reservations, inventory_locations
        )
        issues.extend(reservation_issues)

        origin_side, destination_side, transfer_issues = transfer_impacts(
            normalized.transfers, inventory_locations
        )
        issues.extend(transfer_issues)

        keys = inventory_keys(normalized)
        venues = build_supply(
            supply_by_venue, reservation_map, origin_side, destination_side, keys
        )
        # 没有库存快照的渠道不生成“渠道库存”行，避免把 0 件误读为真实库存；
        # 指向这些渠道的预占/营销已在对应事项中标记为待确认。
        venues = {key: venue for key, venue in venues.items() if venue.batches}
        for key, venue in venues.items():
            if key not in supply_by_venue:
                venue.missing_snapshot = True
            # 预占超过在库：可用量已被压到 0，但缺口本身需要采购/订单侧确认。
            if (
                venue.saleable_gross.high is not None
                and venue.reserved.low > venue.saleable_gross.high
            ):
                issues.append(Issue(
                    code="reservation_exceeds_stock",
                    severity="critical",
                    message=(
                        f"订单预占 {venue.reserved.low:g} 件超过可售在库 "
                        f"{venue.saleable_gross.high:g} 件；可用量按 0 计，"
                        "请订单侧确认超卖或取消预占"
                    ),
                    sku=key[1], channel=key[0],
                    quantity=Range(0.0, venue.reserved.low - venue.saleable_gross.high),
                    source="reservation",
                ))
            if venue.blocked.low > 0:
                issues.append(Issue(
                    code="blocked_stock_present",
                    severity="info",
                    message=(
                        f"有 {venue.blocked.low:g} 件库存处于封锁/隔离状态，不计入可促销"
                        "可用量；若质检放行请更新状态"
                    ),
                    sku=key[1], channel=key[0],
                    quantity=venue.blocked,
                    source="inventory",
                ))

        demands, demand_issues = build_demand(normalized.sales, policy)
        issues.extend(demand_issues)
        for adjustment in demand_adjustments:
            _apply_adjustment(demands, adjustment)

        # 延迟导入避免循环。
        from .demand import assess_venues

        assessments, assess_issues = assess_venues(venues, demands, policy, as_of)
        issues.extend(assess_issues)

        reports, report_issues = build_inventory_reports(assessments, policy, as_of)
        issues.extend(report_issues)
        issues.extend(check_snapshot_freshness(normalized.holdings, policy, as_of))

        plan = plan_marketing(normalized.marketing, assessments, policy)
        issues.extend(plan.issues)

        review = Review(
            as_of=as_of,
            policy=policy,
            reports=reports,
            portfolio=build_portfolio(reports, policy),
            plan=plan,
            issues=issues,
            notes=normalized.notes,
            venues=venues,
            sales_basis=demands,
            holdings=len(normalized.holdings),
            reservations=len(normalized.reservations),
            transfers=len(normalized.transfers),
            resources=len(normalized.marketing),
        )
        _record_unrecognized(review, normalized)
        return review


def _apply_adjustment(demands: dict[tuple[str, str], Demand], adjustment: dict[str, Any]) -> None:
    """按调用方给出的换算系数调整某渠道某 SKU 的日均需求。"""
    key = (adjustment.get("location"), adjustment.get("sku"))
    demand = demands.get(key)  # type: ignore[arg-type]
    if demand is None:
        return
    factor = float(adjustment.get("demand_factor", 1.0))
    demand.daily_low *= factor
    demand.daily_high *= factor


def _record_unrecognized(review: Review, normalized: Normalized) -> None:
    counts: dict[str, int] = defaultdict(int)
    for holding in normalized.holdings:
        if holding.unrecognized_status:
            counts["inventory_status"] += 1
    for transfer in normalized.transfers:
        if transfer.state == "unknown":
            counts["transfer_state"] += 1
    for marketing in normalized.marketing:
        if marketing.state == "unknown":
            counts["marketing_state"] += 1
    review.unrecognized_counts = dict(counts)
