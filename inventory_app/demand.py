"""需求、周转与临期口径。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from .model import Issue, Policy, Range
from .supply import BatchSupply, VenueSupply


@dataclass
class Demand:
    """一个渠道某 SKU 的近期净需求。

    ``daily_low`` / ``daily_high`` 来自期间净销量除以天数，并叠加退货、促销
    投入带来的不确定性。所有覆盖率都以这个区间计算，而不是用单点均值。
    """

    location: str
    sku: str
    units: float = 0.0
    returns: float = 0.0
    spend: float = 0.0
    net: float = 0.0
    period: str | None = None
    days: float = 30.0
    daily_low: float = 0.0
    daily_high: float = 0.0
    sales_basis: str = "none"

    def cover_days(self, quantity: Range) -> tuple[float | None, float | None]:
        """数量范围覆盖的需求天数范围；无需求时返回 ``(None, None)``。"""
        if self.daily_high <= 0 and self.daily_low <= 0:
            return None, None
        high = None if quantity.high is None or self.daily_low <= 0 else quantity.high / self.daily_low
        low = None if self.daily_high <= 0 else quantity.low / self.daily_high
        return low, high


def period_days(period: str) -> int:
    """把 ``YYYY-MM`` 或 ``YYYY-MM-DD..YYYY-MM-DD`` 转为天数。"""
    text = period.strip()
    if ".." in text:
        start, end = text.split("..", 1)
        return max(1, (date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
    try:
        year_text, month_text = text.split("-")[:2]
        year, month = int(year_text), int(month_text)
        if not 1 <= month <= 12:
            raise ValueError(month)
        return _month_days(year, month)
    except (ValueError, IndexError):
        return 30


def _month_days(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days


def build_demand(
    sales: Sequence[dict[str, Any]], policy: Policy,
) -> tuple[dict[tuple[str, str], Demand], list[Issue]]:
    issues: list[Issue] = []
    demands: dict[tuple[str, str], Demand] = {}
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in sales:
        by_key[(row["location"], row["sku"])].append(row)
    for key, rows in by_key.items():
        if len(rows) > 1:
            issues.append(Issue(
                code="multiple_sales_periods",
                severity="info",
                message=(
                    f"同一渠道同一商品有 {len(rows)} 条销售记录；已按天数加权合并，"
                    "不会相加成更高需求"
                ),
                sku=key[1], channel=key[0], source="sales",
            ))
        total_days = 0
        units_total = returns_total = spend_total = 0.0
        periods: list[str] = []
        net_total = 0.0
        for row in rows:
            line = row.get("_line", "?")
            days = period_days(row["period"])
            units = float(row["units"])
            returns = float(row["returns"])
            if returns > units:
                issues.append(Issue(
                    code="returns_exceed_sales",
                    severity="warning",
                    message=f"退货 {returns:g} 大于销售 {units:g}；该期净需求按 0 计，请核对退货期间",
                    sku=row["sku"], channel=row["location"],
                    source=f"sales:{line}",
                ))
            net = max(0.0, units - returns)
            total_days += days
            units_total += units
            returns_total += returns
            spend_total += float(row.get("spend") or 0.0)
            net_total += net
            periods.append(row["period"])
            if days < 14:
                issues.append(Issue(
                    code="short_sales_period",
                    severity="info",
                    message=f"销售期间仅 {days} 天，日均需求波动较大；覆盖率按区间给出",
                    sku=row["sku"], channel=row["location"],
                    source=f"sales:{line}",
                ))
        daily = net_total / total_days
        # 不确定性：退货率与促销投入共同决定区间；多个期间合并时保留区间。
        return_share = 0.0 if units_total <= 0 else min(1.0, returns_total / units_total)
        promo_lift = 0.0
        if spend_total > 0 and net_total > 0:
            promo_lift = min(0.5, spend_total / max(1.0, net_total * 10.0) * 0.1)
        daily_low = daily * (1.0 - return_share) * (1.0 - promo_lift) if daily else 0.0
        daily_high = daily * (1.0 + promo_lift) if daily else 0.0
        demands[key] = Demand(
            location=key[0],
            sku=key[1],
            units=units_total,
            returns=returns_total,
            spend=spend_total,
            net=net_total,
            period=" + ".join(periods),
            days=float(total_days),
            daily_low=daily_low,
            daily_high=daily_high,
            sales_basis="net",
        )
    return demands, issues


@dataclass
class VenueAssessment:
    location: str
    sku: str
    supply: VenueSupply
    demand: Demand
    sellable: Range = field(default_factory=Range.zero)
    cover_low: float | None = None
    cover_high: float | None = None
    days_of_cover: float | None = None
    age_days: float | None = None
    near_expiry: Range = field(default_factory=Range.zero)
    expired: Range = field(default_factory=Range.zero)
    signal: str = "unknown"
    risk: str = "unknown"


def _weighted_age(batches: Sequence[BatchSupply], as_of: date) -> float | None:
    total = 0.0
    weight = 0.0
    for batch in batches:
        base = batch.received
        if base is None:
            continue
        qty = batch.saleable.low
        total += max(0, (as_of - base).days) * qty
        weight += qty
    if weight <= 0:
        return None
    return total / weight


def assess_venues(
    venues: dict[tuple[str, str], VenueSupply],
    demands: dict[tuple[str, str], Demand],
    policy: Policy,
    as_of: date,
) -> tuple[dict[tuple[str, str], VenueAssessment], list[Issue]]:
    issues: list[Issue] = []
    assessments: dict[tuple[str, str], VenueAssessment] = {}
    for key, venue in venues.items():
        demand = demands.get(key) or Demand(location=venue.location, sku=venue.sku)
        if key not in demands:
            issues.append(Issue(
                code="missing_sales_history",
                severity="warning",
                message="该渠道该商品没有净销售记录；覆盖率无法计算，仅给出库存健康信号",
                sku=venue.sku, channel=venue.location,
                source="sales",
            ))
        near = Range.zero()
        expired = Range.zero()
        for batch in venue.batches:
            if batch.expiry is None:
                continue
            if batch.expiry <= as_of:
                expired = expired + batch.saleable
            elif (batch.expiry - as_of).days <= policy.near_expiry_days:
                near = near + batch.saleable
        cover_low, cover_high = demand.cover_days(venue.sellable)
        # 覆盖率高但完全卖不动（例如仓库没有零售需求）时，按需求天数算出的
        # “高覆盖”没有意义；信号应显示“无净需求”，而不是“滞销/临期”。
        if demand.daily_high <= 0 and venue.sellable.low > 0:
            cover_low = cover_high = None
        assessment = VenueAssessment(
            location=venue.location,
            sku=venue.sku,
            supply=venue,
            demand=demand,
            sellable=venue.sellable,
            cover_low=cover_low,
            cover_high=cover_high,
            days_of_cover=None if cover_low is None else cover_low,
            age_days=_weighted_age(venue.batches, as_of),
            near_expiry=near,
            expired=expired,
        )
        assessment.signal, assessment.risk = _classify(assessment, policy)
        if assessment.signal == "missing":
            issues.append(Issue(
                code="missing_demand",
                severity="warning",
                message="没有可用的净需求，无法判断畅销/滞销；需要销售或补货计划",
                sku=venue.sku, channel=venue.location,
            ))
        assessments[key] = assessment
    return assessments, issues


def _classify(assessment: VenueAssessment, policy: Policy) -> tuple[str, str]:
    """返回 ``(健康信号, 风险)``。

    信号与风险是两条正交的线：临期决定信号，覆盖率决定风险。临期库存如果仍
    卖得很快，风险是缺货而不是积压——否则会把“快卖完的临期货”错误地当成
    促销对象。
    """
    if assessment.demand.daily_high <= 0:
        if assessment.sellable.high is not None and assessment.sellable.high > 0:
            return "no_demand", "overstock"
        return "missing", "unknown"
    cover = assessment.cover_low
    if cover is None:
        return "unknown", "unknown"

    near = assessment.near_expiry.high is not None and assessment.near_expiry.high > 0
    if cover < policy.min_cover_days:
        risk = "shortage"
    elif cover > policy.overstock_cover_days:
        risk = "overstock"
    else:
        risk = "balanced"

    if near:
        if risk == "shortage":
            return "near_expiry_tight", risk
        if risk == "overstock":
            return "near_expiry_overstock", risk
        return "near_expiry", risk
    if risk == "shortage":
        return "fast", risk
    if risk == "overstock":
        return "slow", risk
    return "healthy", risk
