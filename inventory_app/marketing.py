"""营销资源分配与保护约束。

规则
----
* **已确认**（``confirmed``）的营销资源永不自动改动；即使新库存显示其会
  造成缺货，也保留原决策并进入人工处理清单。
* **未确认**（``proposed``/``draft``）的资源按优先级重新分配，优先级依次是：
  临期清理 > 滞销去化 > 缺货补充 > 常规增长。每档内部按可用量从多到少排序。
* 每个渠道的可分配上限是当前可确认可售量 ``sellable.low`` 减去最低库存承诺
  （商品团队/财务维护）再减去该渠道已确认营销占用量。使用 ``low`` 是刻意的：
  促销资源不得建立在未确认库存上。
* 预算合计不能超过 ``policy.budget_total``；没有预算数据时给出未校验标记。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from .demand import VenueAssessment
from .model import Issue, MarketingResource, Policy, Range


PRIORITY_LABELS = {
    1: "临期清理",
    2: "滞销去化",
    3: "常规增长",
    4: "缺货渠道·建议转投",
    5: "无需求支撑",
}


@dataclass
class Allocation:
    resource_id: str
    sku: str
    channel: str | None
    requested: float
    protected: bool
    approved: float | None = None
    reason: str = ""
    priority: int | None = None
    shortfall: float = 0.0
    conflict: str | None = None
    budget: float | None = None
    min_stock: float = 0.0
    status: str = "unreviewed"

    def as_dict(self) -> dict:
        return {
            "id": self.resource_id,
            "sku": self.sku,
            "channel": self.channel,
            "requested": round(self.requested, 2),
            "protected": self.protected,
            "approved": None if self.approved is None else round(self.approved, 2),
            "reason": self.reason,
            "priority": self.priority,
            "priority_label": PRIORITY_LABELS.get(self.priority or 0),
            "shortfall": round(self.shortfall, 2),
            "conflict": self.conflict,
            "budget": self.budget,
            "min_stock": round(self.min_stock, 2),
            "status": self.status,
        }


@dataclass
class ChannelTension:
    sku: str
    channel: str
    risk: str
    cover_low: float | None
    cover_high: float | None
    sellable: Range
    protected_qty: float
    message: str

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "channel": self.channel,
            "risk": self.risk,
            "cover_low": None if self.cover_low is None else round(self.cover_low, 1),
            "cover_high": None if self.cover_high is None else round(self.cover_high, 1),
            "sellable": self.sellable.as_dict(),
            "protected_qty": round(self.protected_qty, 2),
            "message": self.message,
        }


@dataclass
class Plan:
    allocations: list[Allocation] = field(default_factory=list)
    tensions: list[ChannelTension] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    budget_total: float | None = None
    budget_committed: float = 0.0
    budget_proposed: float = 0.0
    budget_unverified: bool = False
    released: int = 0


PRIORITY_BY_RISK = {
    "shortage": 4,
    "overstock": 2,
    "balanced": 3,
    "unknown": 3,
    "missing": 5,
}


def _priority_for(assessment: VenueAssessment | None, resource: MarketingResource) -> int:
    if assessment is None:
        return 3
    if assessment.signal in {"near_expiry_overstock", "near_expiry"}:
        return 1
    if assessment.near_expiry.low > 0 and assessment.risk != "shortage":
        return 1
    if assessment.signal in {"slow", "no_demand"}:
        return 2
    if assessment.risk == "shortage":
        return 4
    return PRIORITY_BY_RISK.get(assessment.risk, 3)


def plan_marketing(
    resources: Sequence[MarketingResource],
    assessments: dict[tuple[str, str], VenueAssessment],
    policy: Policy,
) -> Plan:
    plan = Plan(budget_total=policy.budget_total)
    issues: list[Issue] = []

    by_sku: dict[str, list[MarketingResource]] = defaultdict(list)
    for resource in resources:
        by_sku[resource.sku].append(resource)

    # 已确认资源先占用渠道的可分配上限与预算，未确认资源不得挤压它们。
    protected_qty: dict[tuple[str, str], float] = defaultdict(float)
    for resource in resources:
        if resource.channel is None:
            continue
        if resource.protected and resource.qty is not None:
            protected_qty[(resource.channel, resource.sku)] += resource.qty

    budget_verified = policy.budget_total is not None
    if not budget_verified and resources:
        plan.budget_unverified = True
        issues.append(Issue(
            code="budget_missing",
            severity="warning",
            message=(
                "未提供促销预算上限；数量建议已给出，但预算合计未经校验。"
                "财务可在策略中补充预算上限与单位促销成本"
            ),
            assumption=True,
        ))

    for sku, sku_resources in sorted(by_sku.items()):
        # 同一 SKU 的渠道健康度，用于识别“一渠缺货、另一渠积压”。
        venue_states = [
            (assessment, key) for key, assessment in assessments.items() if assessment.sku == sku
        ]
        shortage_channels = [
            a for a, _ in venue_states if a.risk == "shortage"
        ]
        overstock_channels = [
            a for a, _ in venue_states
            if a.risk == "overstock" or a.signal in {"slow", "no_demand"}
        ]
        if shortage_channels and overstock_channels:
            for short in shortage_channels:
                for over in overstock_channels:
                    if short.location == over.location:
                        continue
                    plan.tensions.append(ChannelTension(
                        sku=sku,
                        channel=short.location,
                        risk="transfer_opportunity",
                        cover_low=short.cover_low,
                        cover_high=short.cover_high,
                        sellable=short.sellable,
                        protected_qty=protected_qty.get((short.location, sku), 0.0),
                        message=(
                            f"{policy.label_for(short.location)} 覆盖仅 "
                            f"{_fmt_days(short.cover_low)} 天，"
                            f"{policy.label_for(over.location)} 仍有无零售需求的 "
                            f"{over.sellable.low:g} 件可调；"
                            "促销应先补缺货渠道，避免把积压从一渠搬到另一渠"
                        ),
                    ))

        # 先把已确认资源原样保留，并检查它们是否仍然可行。
        confirmed_on_sku = [r for r in sku_resources if r.protected]
        for resource in confirmed_on_sku:
            allocation = Allocation(
                resource_id=resource.id,
                sku=resource.sku,
                channel=resource.channel,
                requested=resource.qty or 0.0,
                protected=True,
                approved=resource.qty,
                priority=1 if resource.slot and resource.slot <= (policy.as_of or date.max) else None,
                budget=resource.budget,
                status="protected",
                reason="已确认的营销资源，保留原决策",
            )
            plan.budget_committed += resource.budget or 0.0
            if resource.channel is None:
                issues.append(Issue(
                    code="confirmed_without_channel",
                    severity="critical",
                    message=f"已确认营销资源 {resource.id} 未指定渠道，需人工确认投放对象",
                    sku=sku, source=f"marketing:{resource.id}",
                ))
                allocation.conflict = "渠道缺失"
                allocation.status = "manual"
                plan.allocations.append(allocation)
                continue
            assessment = assessments.get((resource.channel, sku))
            if assessment is None:
                issues.append(Issue(
                    code="confirmed_without_inventory",
                    severity="critical",
                    message=(
                        f"已确认营销资源 {resource.id} 指向 {resource.channel}，"
                        "但该渠道没有库存快照；保留原决策，转人工确认补货或调整档期"
                    ),
                    sku=sku, channel=resource.channel,
                    source=f"marketing:{resource.id}",
                ))
                allocation.conflict = "渠道无库存快照"
                allocation.status = "manual"
            else:
                floor = max(
                    policy.floor_for(sku, resource.channel),
                    resource.min_stock or 0.0,
                )
                available_low = max(0.0, assessment.sellable.low - floor)
                if resource.qty is not None and resource.qty > available_low:
                    gap = resource.qty - available_low
                    allocation.shortfall = gap
                    allocation.conflict = "渠道总可售不足以覆盖已确认资源"
                    allocation.status = "manual"
                    allocation.reason = (
                        f"保留原 {resource.qty:g} 件的已确认决策；缺口 {gap:g} 件待补货或调整档期"
                    )
                    issues.append(Issue(
                        code="protected_resource_shortfall",
                        severity="critical",
                        message=(
                            f"已确认营销资源 {resource.id} 需要 {resource.qty:g} 件，"
                            f"扣除最低库存 {floor:g} 后该渠道可确认仅 {available_low:g} 件，"
                            f"缺口 {gap:g} 件；保留原决策并转人工处理（补货或调整档期）"
                        ),
                        sku=sku, channel=resource.channel,
                        quantity=Range(0.0, gap),
                        source=f"marketing:{resource.id}",
                    ))
                    if assessment.risk == "shortage":
                        plan.tensions.append(ChannelTension(
                            sku=sku,
                            channel=resource.channel,
                            risk="protected_overcommit",
                            cover_low=assessment.cover_low,
                            cover_high=assessment.cover_high,
                            sellable=assessment.sellable,
                            protected_qty=protected_qty.get((resource.channel, sku), 0.0),
                            message=(
                                f"{policy.label_for(resource.channel)} 已确认营销 "
                                f"{resource.qty:g} 件，但渠道覆盖仅 "
                                f"{_fmt_days(assessment.cover_low)} 天；促销会加重缺货"
                            ),
                        ))
            plan.allocations.append(allocation)

        # 未确认资源按优先级重排。优先级越高越先获得剩余可分配量。
        proposed = [r for r in sku_resources if not r.protected and r.state != "cancelled"]
        proposed.sort(key=lambda r: (
            _priority_for(assessments.get((r.channel or "", sku)), r),
            r.slot or date.max,
            r.id,
        ))
        consumed: dict[tuple[str, str], float] = defaultdict(float)
        for resource in proposed:
            allocation = Allocation(
                resource_id=resource.id,
                sku=resource.sku,
                channel=resource.channel,
                requested=resource.qty or 0.0,
                protected=False,
                budget=resource.budget,
                status="proposed",
            )
            if resource.state == "cancelled":
                allocation.status = "released"
                allocation.reason = "已取消，不占用资源"
                plan.allocations.append(allocation)
                continue
            if resource.channel is None:
                allocation.status = "manual"
                allocation.conflict = "渠道缺失"
                allocation.reason = "未确认资源缺少渠道，需人工分配"
                issues.append(Issue(
                    code="unconfirmed_without_channel",
                    severity="warning",
                    message=f"未确认营销资源 {resource.id} 未指定渠道，无法自动分配",
                    sku=sku, source=f"marketing:{resource.id}",
                ))
                plan.allocations.append(allocation)
                continue
            assessment = assessments.get((resource.channel, sku))
            if assessment is None:
                allocation.status = "released"
                allocation.approved = 0.0
                allocation.reason = "渠道无库存快照，未确认资源暂不分配"
                plan.released += 1
                plan.allocations.append(allocation)
                issues.append(Issue(
                    code="unconfirmed_without_inventory",
                    severity="info",
                    message=(
                        f"未确认营销资源 {resource.id} 指向无库存快照的渠道，"
                        "已释放；补报库存后可重新分配"
                    ),
                    sku=sku, channel=resource.channel,
                    source=f"marketing:{resource.id}",
                ))
                continue

            allocation.priority = _priority_for(assessment, resource)
            allocation.conflict = None
            floor = max(
                policy.floor_for(sku, resource.channel),
                resource.min_stock or 0.0,
                assessment.demand.daily_high * policy.min_cover_days,
            )
            capacity = max(
                0.0,
                assessment.sellable.low
                - floor
                - protected_qty.get((resource.channel, sku), 0.0)
                - consumed[(resource.channel, sku)],
            )
            allocation.min_stock = floor
            requested = resource.qty or 0.0
            approved = min(requested, capacity)
            if assessment.risk == "shortage":
                # 缺货渠道不使用促销资源；保留申请量留作转投/改期参考。
                approved = 0.0
                allocation.approved = 0.0
                allocation.min_stock = floor
                allocation.conflict = "缺口渠道不投放"
                allocation.reason = (
                    f"该渠道覆盖 {_fmt_days(assessment.cover_low)} 天，低于最低覆盖 "
                    f"{policy.min_cover_days:g} 天；建议转投积压渠道或推迟档期"
                )
                allocation.status = "manual"
                plan.released += 1
                issues.append(Issue(
                    code="promotion_into_shortage",
                    severity="warning",
                    message=(
                        f"未确认营销资源 {resource.id} 申请 {requested:g} 件投向缺货渠道 "
                        f"{resource.channel}；已暂缓，建议转投积压渠道或推迟档期"
                    ),
                    sku=sku, channel=resource.channel,
                    quantity=Range(0.0, requested),
                    source=f"marketing:{resource.id}",
                ))
                plan.allocations.append(allocation)
                continue
            if approved < requested:
                allocation.shortfall = requested - approved
                allocation.reason = (
                    f"可分配上限 {capacity:g} 件（可售 {assessment.sellable.low:g} "
                    f"− 最低库存/安全覆盖 {floor:g} − 已确认营销 "
                    f"{protected_qty.get((resource.channel, sku), 0.0):g}）"
                )
                plan.released += 1
                issues.append(Issue(
                    code="resource_scaled_back",
                    severity="warning" if approved > 0 else "info",
                    message=(
                        f"未确认营销资源 {resource.id} 由 {requested:g} 件下调为 "
                        f"{approved:g} 件：{allocation.reason}；库存修订后可重排"
                    ),
                    sku=sku, channel=resource.channel,
                    quantity=Range(0.0, requested - approved),
                    source=f"marketing:{resource.id}",
                ))
            allocation.approved = approved
            consumed[(resource.channel, sku)] += approved
            if allocation.status == "manual":
                plan.allocations.append(allocation)
                continue
            plan.budget_proposed += resource.budget or 0.0
            plan.allocations.append(allocation)

    if budget_verified:
        total = plan.budget_committed + plan.budget_proposed
        if total > policy.budget_total:  # type: ignore[operator]
            issues.append(Issue(
                code="budget_exceeded",
                severity="critical",
                message=(
                    f"已确认预算 {plan.budget_committed:g} + 拟投放预算 "
                    f"{plan.budget_proposed:g} 超过促销预算上限 {policy.budget_total:g}"
                ),
                assumption=False,
            ))
            _trim_to_budget(plan, policy.budget_total)
        else:
            issues.append(Issue(
                code="budget_ok",
                severity="info",
                message=(
                    f"预算合计 {total:g}，在促销预算上限 {policy.budget_total:g} 以内"
                ),
            ))

    plan.issues = issues
    return plan


def _trim_to_budget(plan: Plan, budget_total: float) -> None:
    """从优先级最低的未确认资源开始释放，已确认资源不动。"""
    remaining = budget_total - plan.budget_committed
    for allocation in sorted(
        (a for a in plan.allocations if not a.protected),
        key=lambda a: (-(a.priority or 0), a.resource_id),
    ):
        cost = allocation.budget or 0.0
        if cost <= remaining:
            remaining -= cost
            continue
        allocation.approved = 0.0
        allocation.status = "released"
        allocation.shortfall = allocation.requested
        allocation.reason = (
            (allocation.reason + "；" if allocation.reason else "")
            + "预算不足，已释放等待重新排期"
        )
        plan.released += 1


def _fmt_days(value: float | None) -> str:
    return "未知" if value is None else f"{value:.1f}"
