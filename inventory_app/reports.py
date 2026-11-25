"""供应链口径检查：库存龄、覆盖、缺报与渠道张力汇总。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from .demand import VenueAssessment
from .model import Holding, Issue, Policy, Range


@dataclass
class VenueReport:
    """单个渠道单个 SKU 的评审视图。"""

    location: str
    sku: str
    label: str
    gross: Range
    saleable: Range
    blocked: Range
    reserved: Range
    origin_uncertain: Range
    destination_possible: Range
    sellable: Range
    cover_low: float | None
    cover_high: float | None
    age_days: float | None
    near_expiry: Range
    expired: Range
    net_demand: float
    daily_low: float
    daily_high: float
    signal: str
    risk: str
    replenish_to_safety: Range
    batches: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "channel": self.location,
            "channel_label": self.label,
            "sku": self.sku,
            "gross": self.gross.as_dict(),
            "saleable": self.saleable.as_dict(),
            "blocked": self.blocked.as_dict(),
            "reserved": self.reserved.as_dict(),
            "in_transit_deduction": self.origin_uncertain.as_dict(),
            "possible_double_count": self.destination_possible.as_dict(),
            "sellable": self.sellable.as_dict(),
            "cover_days_low": _round(self.cover_low),
            "cover_days_high": _round(self.cover_high),
            "age_days": _round(self.age_days),
            "near_expiry": self.near_expiry.as_dict(),
            "expired": self.expired.as_dict(),
            "net_demand": round(self.net_demand, 1),
            "daily_demand_low": round(self.daily_low, 2),
            "daily_demand_high": round(self.daily_high, 2),
            "signal": self.signal,
            "risk": self.risk,
            "replenish_to_safety": self.replenish_to_safety.as_dict(),
            "batches": self.batches,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def build_inventory_reports(
    assessments: dict[tuple[str, str], VenueAssessment],
    policy: Policy,
    as_of: date,
) -> tuple[list[VenueReport], list[Issue]]:
    reports: list[VenueReport] = []
    issues: list[Issue] = []
    for (location, sku), assessment in sorted(assessments.items()):
        venue = assessment.supply
        demand = assessment.demand
        safety_qty = demand.daily_high * policy.safety_cover_days
        replenish = Range.exact(safety_qty) - assessment.sellable
        reports.append(VenueReport(
            location=location,
            sku=sku,
            label=policy.label_for(location),
            gross=venue.gross,
            saleable=venue.saleable_gross,
            blocked=venue.blocked,
            reserved=venue.reserved,
            origin_uncertain=venue.origin_uncertain,
            destination_possible=venue.destination_possible,
            sellable=assessment.sellable,
            cover_low=assessment.cover_low,
            cover_high=assessment.cover_high,
            age_days=assessment.age_days,
            near_expiry=assessment.near_expiry,
            expired=assessment.expired,
            net_demand=demand.net,
            daily_low=demand.daily_low,
            daily_high=demand.daily_high,
            signal=assessment.signal,
            risk=assessment.risk,
            replenish_to_safety=replenish,
            batches=[
                {
                    "batch": batch.batch,
                    "expiry": None if batch.expiry is None else batch.expiry.isoformat(),
                    "received": None if batch.received is None else batch.received.isoformat(),
                    "observed": batch.observed.as_dict(),
                    "saleable": batch.saleable.as_dict(),
                    "status": batch.status,
                    "age_days": None if batch.received is None else (as_of - batch.received).days,
                    "days_to_expiry": None if batch.expiry is None else (batch.expiry - as_of).days,
                }
                for batch in venue.batches
            ],
        ))
    return reports, issues


@dataclass
class Portfolio:
    """按 SKU 汇集的库存健康与渠道张力，用于资源评审。"""

    sku: str
    total_sellable: Range = field(default_factory=Range.zero)
    total_blocked: Range = field(default_factory=Range.zero)
    channels: list[VenueReport] = field(default_factory=list)
    tension: str = "none"
    tension_detail: list[dict] = field(default_factory=list)


def build_portfolio(reports: Sequence[VenueReport], policy: Policy) -> list[Portfolio]:
    by_sku: dict[str, list[VenueReport]] = defaultdict(list)
    for report in reports:
        by_sku[report.sku].append(report)
    out: list[Portfolio] = []
    for sku, entries in sorted(by_sku.items()):
        portfolio = Portfolio(sku=sku, channels=list(entries))
        for entry in entries:
            portfolio.total_sellable = portfolio.total_sellable + entry.sellable
            portfolio.total_blocked = portfolio.total_blocked + entry.blocked
        shortage = [e for e in entries if e.risk == "shortage"]
        overstock = [e for e in entries if e.risk == "overstock" or e.signal in {"slow", "no_demand"}]
        if shortage and overstock:
            portfolio.tension = "mixed"
            for short in shortage:
                for over in overstock:
                    if short.location == over.location:
                        continue
                    portfolio.tension_detail.append({
                        "shortage_channel": short.location,
                        "shortage_label": short.label,
                        "shortage_cover_days": _round(short.cover_low),
                        "overstock_channel": over.location,
                        "overstock_label": over.label,
                        "overstock_cover_days": _round(over.cover_low),
                        "sellable_in_overstock_channel": over.sellable.as_dict(),
                        "message": (
                            f"{short.label} 预计 {_fmt(short.cover_low)} 天售罄，"
                            f"{over.label} 有 {over.sellable.low:g} 件可调（{_overstock_phrase(over)}）；"
                            "同一商品在两个渠道方向相反"
                        ),
                    })
        elif shortage:
            portfolio.tension = "shortage"
        elif overstock:
            portfolio.tension = "overstock"
        out.append(portfolio)
    return out


def _fmt(value: float | None) -> str:
    return "未知" if value is None else f"{value:.1f}"


def _overstock_phrase(report: VenueReport) -> str:
    if report.cover_low is None:
        return "无零售净需求" if report.signal == "no_demand" else "覆盖未知"
    return f"覆盖 {report.cover_low:.1f} 天"


def check_snapshot_freshness(
    holdings: Sequence[Holding], policy: Policy, as_of: date,
) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[tuple[str, str, str]] = set()
    for holding in holdings:
        key = (holding.location, holding.sku, holding.batch)
        if key in seen:
            continue
        seen.add(key)
        age = (as_of - holding.day).days
        if age > policy.stale_snapshot_days:
            issues.append(Issue(
                code="stale_inventory_snapshot",
                severity="warning",
                message=(
                    f"库存快照距评审日 {age} 天，超过 {policy.stale_snapshot_days} 天阈值；"
                    "可售量与覆盖率需按最新盘点复核"
                ),
                sku=holding.sku, channel=holding.location, batch=holding.batch,
                source=f"inventory:{holding.line}",
            ))
        if holding.day > as_of:
            issues.append(Issue(
                code="future_inventory_snapshot",
                severity="warning",
                message="库存快照日期晚于评审日；请确认是否误填",
                sku=holding.sku, channel=holding.location, batch=holding.batch,
                source=f"inventory:{holding.line}",
            ))
    return issues
