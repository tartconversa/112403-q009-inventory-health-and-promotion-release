"""事件驱动的库存修订与前后对比。

支持三类后续数据，都可以直接来自运营或仓储系统：

``count`` / ``盘点`` / ``盘点差异``
    以实际清点数量替换指定批次的库存快照（生成新版本，不覆盖历史）。
``return`` / ``退货``
    把退回货品计入指定渠道；退货同时下调该渠道近期净需求，避免一边补货
    一边按旧销量放大促销。
``receipt`` / ``调拨签收`` / ``签收``
    把在途调拨标记为已签收，并在接收方没有该批次时补一条已签收的库存记录。

事件只是输入增量。引擎每次都会从头重算，因此“已确认的营销安排保留原决策并
进入人工处理”不依赖任何状态记忆：已确认资源在任何一次重算中都受保护。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from .engine import InventoryEngine, Review
from .inputs import Snapshot
from .model import Issue, Range

COUNT_ALIASES = {"count", "stocktake", "cycle-count", "inventory-count", "盘点", "盘点差异"}
RETURN_ALIASES = {"return", "returns", "退货"}
RECEIPT_ALIASES = {"receipt", "transfer-receipt", "grn", "签收", "调拨签收", "收货"}


def _kind(value: Any) -> str:
    token = str(value or "").strip().lower().replace("_", "-")
    if token in COUNT_ALIASES:
        return "count"
    if token in RETURN_ALIASES:
        return "return"
    if token in RECEIPT_ALIASES:
        return "receipt"
    return ""


@dataclass
class Change:
    """一次修订前后同一渠道同一 SKU 的变化。"""

    sku: str
    channel: str
    before_sellable: Range | None = None
    after_sellable: Range | None = None
    before_risk: str | None = None
    after_risk: str | None = None
    before_signal: str | None = None
    after_signal: str | None = None
    before_cover: float | None = None
    after_cover: float | None = None

    @property
    def sellable_delta(self) -> float | None:
        if self.before_sellable is None or self.after_sellable is None:
            return None
        return round(self.after_sellable.low - self.before_sellable.low, 2)

    @property
    def risk_changed(self) -> bool:
        return self.before_risk != self.after_risk

    @property
    def range_changed(self) -> bool:
        """低端或高端任一变化都算变化；缺货缺口常体现在高端。"""
        if self.before_sellable is None or self.after_sellable is None:
            return self.before_sellable is not self.after_sellable
        before, after = self.before_sellable, self.after_sellable
        return (
            round(before.low, 2) != round(after.low, 2)
            or before.high != after.high
        )

    @property
    def significant(self) -> bool:
        return self.range_changed or self.risk_changed

    def as_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "channel": self.channel,
            "sellable_before": None if self.before_sellable is None else self.before_sellable.as_dict(),
            "sellable_after": None if self.after_sellable is None else self.after_sellable.as_dict(),
            "sellable_delta_low": self.sellable_delta,
            "risk_before": self.before_risk,
            "risk_after": self.after_risk,
            "signal_before": self.before_signal,
            "signal_after": self.after_signal,
            "cover_before": None if self.before_cover is None else round(self.before_cover, 2),
            "cover_after": None if self.after_cover is None else round(self.after_cover, 2),
        }


@dataclass
class Revision:
    """一次事件应用后的结果。"""

    events: list[dict[str, Any]]
    snapshot: Snapshot
    before: Review
    after: Review
    changes: list[Change] = field(default_factory=list)
    allocation_changes: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events_applied": [self._event_summary(e) for e in self.events],
            "inventory_changes": [
                c.as_dict() for c in self.changes if c.significant
            ],
            "allocation_changes": self.allocation_changes,
            "manual_items_after": self.after.manual_items,
            "issues": [i.as_dict() for i in self.issues],
            "summary": {
                "risk_changed": sum(1 for c in self.changes if c.risk_changed),
                "allocations_changed": len(self.allocation_changes),
                "protected_moved_to_manual": len(self.after.manual_items),
            },
        }

    @staticmethod
    def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": event.get("id"),
            "kind": event.get("kind"),
            "sku": event.get("sku"),
            "channel": event.get("location"),
            "batch": event.get("batch"),
            "qty": event.get("qty"),
            "day": None if event.get("day") is None else event["day"].isoformat(),
        }


def apply_events(
    snapshot: Snapshot,
    events: Sequence[dict[str, Any]],
    engine: InventoryEngine,
) -> Revision:
    """在快照上应用盘点/退货/签收事件，并给出新旧两次评审的差异。"""
    before = engine.review(snapshot)
    # 事件来自盘点/退货/签收系统，本身带有商品与渠道；这些新行必须带有显式
    # 状态，否则会被当成“来源未提供状态列”的假设值。
    columns = snapshot.inventory_columns | {"status", "received"}
    inventory = [dict(row) for row in snapshot.inventory]
    status_supplied = "status" in snapshot.inventory_columns
    for row in inventory:
        row.setdefault("received", None)
        if not status_supplied:
            # 原来源没有状态列时，补上与评审一致的默认状态，避免新增事件行
            # 触发“状态列存在但留空”的更严格口径。
            row["status"] = row.get("status") or engine.policy.default_inventory_status
    commitments = [dict(row) for row in snapshot.commitments]
    sales = [dict(row) for row in snapshot.sales]
    issues: list[Issue] = []

    for event in events:
        kind = _kind(event.get("kind"))
        day = event.get("day") or before.as_of
        if not event.get("sku"):
            issues.append(Issue(
                code="event_missing_sku", severity="warning",
                message=f"事件 {event.get('id')} 缺少 sku，已跳过",
                source=f"event:{event.get('id')}",
            ))
            continue
        if kind == "count":
            _apply_count(inventory, event, issues, day)
        elif kind == "return":
            _apply_return(inventory, sales, event, issues, day)
        elif kind == "receipt":
            _apply_receipt(inventory, commitments, event, issues, day)
        else:
            issues.append(Issue(
                code="unknown_event_kind", severity="warning",
                message=f"事件类型 {event.get('kind')!r} 无法识别，已跳过",
                sku=event.get("sku"), source=f"event:{event.get('id')}",
            ))

    revised = Snapshot(
        inventory=inventory,
        commitments=commitments,
        sales=sales,
        notes=snapshot.notes,
        inventory_columns=columns,
    )
    after = engine.review(revised)
    return Revision(
        events=list(events), snapshot=revised, before=before, after=after,
        changes=_diff_inventory(before, after),
        allocation_changes=_diff_allocations(before, after),
        issues=issues,
    )


def _apply_count(
    inventory: list[dict[str, Any]], event: dict[str, Any],
    issues: list[Issue], default_day: date,
) -> None:
    sku = event["sku"]
    location = event.get("location")
    batch = event.get("batch")
    qty = event.get("qty")
    if location is None:
        issues.append(Issue(
            code="count_missing_location", severity="warning",
            message=f"盘点事件 {event.get('id')} 缺少渠道，无法定位批次",
            sku=sku, source=f"event:{event.get('id')}",
        ))
        return
    if qty is None:
        issues.append(Issue(
            code="count_missing_qty", severity="warning",
            message=f"盘点事件 {event.get('id')} 缺少实盘数量，无法修订",
            sku=sku, channel=location, batch=batch,
            source=f"event:{event.get('id')}",
        ))
        return
    matches = [
        row for row in inventory
        if row["sku"] == sku and row["location"] == location
        and (batch is None or row["batch"] == batch)
    ]
    if not matches:
        issues.append(Issue(
            code="count_without_snapshot", severity="warning",
            message=(
                f"盘点事件 {event.get('id')} 对应 {location}/{sku}/{batch or '全部批次'} "
                "没有库存快照；按新增盘点结果处理"
            ),
            sku=sku, channel=location, batch=batch,
            source=f"event:{event.get('id')}",
        ))
        inventory.append({
            "location": location, "sku": sku, "batch": batch or f"CNT-{event.get('id')}",
            "qty": float(qty), "expiry": None,
            "day": event.get("day") or default_day,
            "status": event.get("state") or "saleable", "received": None,
            "substitute_for": None, "note": f"盘点 {event.get('id')}", "_line": -1,
        })
        return
    target = max(matches, key=lambda row: row["day"])
    before_qty = target.get("qty")
    effective_day = event.get("day") or default_day
    if effective_day == target["day"]:
        # 同一天改数量 = 修正原快照（例如发现录入错误），而不是新增一次盘点。
        target["qty"] = float(qty)
    else:
        # 不同日期 = 新的盘点版本，旧快照保留为历史，由引擎只取最新一版。
        inventory.append(dict(
            target,
            qty=float(qty),
            day=effective_day,
            note=f"盘点 {event.get('id')} 修订",
            _line=-1,
        ))
    target = max(
        (row for row in inventory if row["sku"] == sku and row["location"] == location
         and (batch is None or row["batch"] == batch)),
        key=lambda row: row["day"],
    )
    if event.get("state"):
        target["status"] = event["state"]
    if before_qty is None or float(before_qty) != float(qty):
        issues.append(Issue(
            code="count_variance",
            severity="info",
            message=(
                f"盘点 {event.get('id')} 将 {location}/{sku}/{batch or target['batch']} "
                f"由 {('缺报' if before_qty is None else f'{float(before_qty):g}')} 件"
                f"修订为 {float(qty):g} 件；库存风险与推荐分配已按新数量重算"
            ),
            sku=sku, channel=location, batch=target["batch"],
            source=f"event:{event.get('id')}",
        ))


def _apply_return(
    inventory: list[dict[str, Any]], sales: list[dict[str, Any]],
    event: dict[str, Any], issues: list[Issue], default_day: date,
) -> None:
    sku = event["sku"]
    location = event.get("location")
    qty = event.get("qty")
    if location is None or qty is None:
        issues.append(Issue(
            code="return_incomplete", severity="warning",
            message=f"退货事件 {event.get('id')} 缺少渠道或数量，无法计入",
            sku=sku, source=f"event:{event.get('id')}",
        ))
        return
    batch = event.get("batch") or f"RET-{event.get('id')}"
    inventory.append({
        "location": location, "sku": sku, "batch": batch, "qty": float(qty),
        "expiry": None, "day": event.get("day") or default_day,
        "status": event.get("state") or "saleable",
        "received": None, "substitute_for": None,
        "note": f"退货 {event.get('id')}", "_line": -1,
    })
    # 退货增加当期退货数，净需求下调；避免按旧销量继续放大促销。
    for row in sales:
        if row["sku"] == sku and row["location"] == location:
            row["returns"] = min(float(row["units"]), float(row["returns"]) + float(qty))
    issues.append(Issue(
        code="return_recorded", severity="info",
        message=(
            f"退货 {qty:g} 件已计入 {location}/{sku} 库存，该渠道退货数上调（不超过销量）；"
            "若退货不可再售，请补一条封锁状态的盘点结果"
        ),
        sku=sku, channel=location,
        source=f"event:{event.get('id')}",
    ))


def _apply_receipt(
    inventory: list[dict[str, Any]], commitments: list[dict[str, Any]],
    event: dict[str, Any], issues: list[Issue], default_day: date,
) -> None:
    sku = event["sku"]
    location = event.get("location")
    batch = event.get("batch")
    qty = event.get("qty")
    transfer_id = event.get("target") or event.get("id")
    matched = None
    for row in commitments:
        if row.get("id") == transfer_id and str(row.get("kind", "")).lower().replace("_", "-") in {
            "transfer", "in-transit", "intransit", "movement", "调拨", "在途",
        }:
            matched = row
            break
    qty_supplied = qty is not None
    if matched is not None:
        matched["state"] = "received"
        matched["day"] = event.get("day") or default_day
        if location is None:
            location = matched.get("to")
        # 只有调用方显式给出签收数量时才补库存；否则只把调拨改为已签收，
        # 接收方数量仍以它自己的库存快照为准，避免凭空增加库存。
        if not qty_supplied:
            qty = None
    if location is None:
        issues.append(Issue(
            code="receipt_missing_location", severity="warning",
            message=f"签收事件 {event.get('id')} 无法确定接收方渠道，未计入库存",
            sku=sku, source=f"event:{event.get('id')}",
        ))
        return
    if qty in (None, 0):
        issues.append(Issue(
            code="receipt_missing_qty", severity="info",
            message=f"签收事件 {event.get('id')} 未提供数量；仅更新调拨状态",
            sku=sku, channel=location, source=f"event:{event.get('id')}",
        ))
    else:
        inventory.append({
            "location": location, "sku": sku, "batch": batch or f"RCV-{event.get('id')}",
            "qty": float(qty), "expiry": None,
            "day": event.get("day") or default_day,
            "status": event.get("state") or "saleable",
            "received": event.get("day") or default_day,
            "substitute_for": None, "note": f"调拨签收 {transfer_id}", "_line": -1,
        })
    issues.append(Issue(
        code="receipt_recorded", severity="info",
        message=(
            f"调拨 {transfer_id} 已签收；重复计入风险解除，"
            "接收方可用量按签收数量重新计算"
        ),
        sku=sku, channel=location, source=f"event:{event.get('id')}",
    ))


def _diff_inventory(before: Review, after: Review) -> list[Change]:
    def index(review: Review) -> dict[tuple[str, str], Any]:
        return {(report.location, report.sku): report for report in review.reports}

    old, new = index(before), index(after)
    changes: list[Change] = []
    for key in sorted(set(old) | set(new)):
        old_report = old.get(key)
        new_report = new.get(key)
        changes.append(Change(
            sku=key[1],
            channel=key[0],
            before_sellable=None if old_report is None else old_report.sellable,
            after_sellable=None if new_report is None else new_report.sellable,
            before_risk=None if old_report is None else old_report.risk,
            after_risk=None if new_report is None else new_report.risk,
            before_signal=None if old_report is None else old_report.signal,
            after_signal=None if new_report is None else new_report.signal,
            before_cover=None if old_report is None else old_report.cover_low,
            after_cover=None if new_report is None else new_report.cover_low,
        ))
    return changes


def _diff_allocations(before: Review, after: Review) -> list[dict[str, Any]]:
    old = {a.resource_id: a for a in before.plan.allocations}
    out: list[dict[str, Any]] = []
    for allocation in after.plan.allocations:
        previous = old.get(allocation.resource_id)
        if previous is None:
            out.append({
                "id": allocation.resource_id,
                "change": "new",
                "before": None,
                "after": allocation.as_dict(),
            })
            continue
        if (
            previous.approved != allocation.approved
            or previous.status != allocation.status
            or previous.conflict != allocation.conflict
        ):
            out.append({
                "id": allocation.resource_id,
                "change": "protected_review" if allocation.protected else "reallocated",
                "before": previous.as_dict(),
                "after": allocation.as_dict(),
            })
    return out
