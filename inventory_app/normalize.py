"""把原始快照规范化为引擎输入，并记录所有需要人工确认的事项。

这一层不做任何跨来源的加减，只负责：

* 识别 ``kind`` 决定一条 commitment 是预占、调拨还是营销资源；
* 识别库存状态（可售/封锁/待确认），把缺失状态转为范围；
* 识别调拨是否已在接收方报表中重复上报；
* 校验必填的渠道/批次组合，并把问题转成可确认的 :class:`Issue` 而不是静默丢弃。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .inputs import Snapshot
from .model import (
    Holding,
    Issue,
    MarketingResource,
    Policy,
    Range,
    Reservation,
    Transfer,
    marketing_state,
    normalize_token,
    reservation_effect,
    status_class,
    transfer_state,
)

RESERVATION_KINDS = {
    "reservation", "reserve", "order", "order-reservation", "preorder",
    "预占", "订单预占", "锁库",
}
TRANSFER_KINDS = {"transfer", "in-transit", "intransit", "movement", "调拨", "在途"}
MARKETING_KINDS = {
    "marketing", "promotion", "campaign", "resource", "plan",
    "营销", "促销", "营销资源", "投放",
}
RETURN_KINDS = {"return", "returns", "退货"}
COUNT_KINDS = {"count", "stocktake", "inventory-count", "cycle-count", "盘点", "盘点差异"}
RECEIPT_KINDS = {"receipt", "transfer-receipt", "grn", "签收", "调拨签收", "收货"}


def _classify_kind(raw: str | None) -> str:
    token = normalize_token(raw)
    if token is None:
        return "unknown"
    token = token.replace("_", "-")
    if token in RESERVATION_KINDS:
        return "reservation"
    if token in TRANSFER_KINDS:
        return "transfer"
    if token in MARKETING_KINDS:
        return "marketing"
    return "unknown"


@dataclass
class Normalized:
    holdings: list[Holding] = field(default_factory=list)
    reservations: list[Reservation] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    marketing: list[MarketingResource] = field(default_factory=list)
    sales: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    as_of: date | None = None
    notes: list[str] = field(default_factory=list)


def _holding_from_row(
    row: dict[str, Any], policy: Policy, *, status_column: bool,
) -> tuple[Holding | None, list[Issue]]:
    issues: list[Issue] = []
    line = row["_line"]
    location = row["location"]
    sku = row["sku"]
    batch = row["batch"]
    qty = row.get("qty")
    raw_status = row.get("status")
    status, unrecognized = status_class(raw_status)
    defaulted = False
    if raw_status is None and not status_column and policy.default_inventory_status:
        # 来源根本没有上报状态列：这是“未提供该维度的来源”，与“报了但这一行
        # 空着”不同。按策略的默认状态处理并列为假设，而不是把全部库存设为未知。
        default_status, _ = status_class(policy.default_inventory_status)
        if default_status != "unconfirmed":
            status = default_status
            defaulted = True
    if unrecognized is not None:
        issues.append(Issue(
            code="unrecognized_inventory_status",
            severity="warning",
            message=f"库存状态 {unrecognized!r} 无法识别，按待确认处理（可售 0 至全部）",
            sku=sku, channel=location, batch=batch, source=f"inventory:{line}",
        ))
    elif raw_status is None and not defaulted:
        issues.append(Issue(
            code="missing_inventory_status",
            severity="warning",
            message="库存上报了状态列但该行留空；按待确认列出可售范围，供应链确认后可收窄",
            sku=sku, channel=location, batch=batch,
            quantity=None if qty is None else Range(0.0, qty),
            source=f"inventory:{line}",
        ))
    elif defaulted:
        issues.append(Issue(
            code="inventory_status_assumed",
            severity="info",
            message=(
                f"库存来源未提供可售状态列，暂按 {policy.default_inventory_status!r} 处理；"
                "供应链确认状态后请按实际值重跑"
            ),
            sku=sku, channel=location, batch=batch, source=f"inventory:{line}",
            assumption=True,
        ))
    if qty is None:
        issues.append(Issue(
            code="missing_inventory_qty",
            severity="warning",
            message="批次库存数量缺报；已确认数量按 0 计，可售上限未知",
            sku=sku, channel=location, batch=batch, source=f"inventory:{line}",
        ))
    if qty is not None and qty < 0:
        issues.append(Issue(
            code="negative_inventory_qty",
            severity="warning",
            message=f"批次库存上报为负数（{qty:g}），通常表示账实不符；已按待盘点处理",
            sku=sku, channel=location, batch=batch,
            quantity=None,
            source=f"inventory:{line}",
        ))
    if row.get("expiry") is None:
        issues.append(Issue(
            code="missing_expiry",
            severity="info",
            message="批次缺失效期，无法判断临期；商品团队确认前不计入临期",
            sku=sku, channel=location, batch=batch, source=f"inventory:{line}",
        ))
    saleable_low = 1.0 if status == "saleable" else 0.0
    saleable_high = 1.0 if status in {"saleable", "unconfirmed"} else 0.0
    holding = Holding(
        location=location,
        sku=sku,
        batch=batch,
        qty=qty,
        status_raw=raw_status,
        status=status,
        unrecognized_status=unrecognized,
        expiry=row.get("expiry"),
        received=row.get("received"),
        day=row["day"],
        substitute_for=row.get("substitute_for"),
        status_supplied=raw_status is not None,
        line=line,
        note=row.get("note"),
        saleable_ratio_low=saleable_low,
        saleable_ratio_high=saleable_high,
    )
    return holding, issues


def _dedupe_commitments(
    rows: list[dict[str, Any]], issues: list[Issue],
) -> list[dict[str, Any]]:
    """同一条承诺/预占/调拨可能多次上报，只取最新一版。

    同一天出现内容不同的两版时保留最后一行并标记冲突，绝不把两版相加。
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for row in rows:
        ident = row.get("id") or f"_line{row['_line']}"
        if ident not in grouped:
            order.append(ident)
        grouped[ident].append(row)
    merged: list[dict[str, Any]] = []
    for ident in order:
        versions = grouped[ident]
        latest_day = max((v.get("day") or date.min) for v in versions)
        latest = [v for v in versions if (v.get("day") or date.min) == latest_day]
        chosen = latest[-1]
        if len({(v.get("state"), v.get("qty"), v.get("kind")) for v in latest}) > 1:
            issues.append(Issue(
                code="conflicting_commitment_versions",
                severity="warning",
                message=(
                    f"记录 {ident} 在同一天有 {len(latest)} 个不同版本；"
                    "已取最后一行，请供应链确认最终状态"
                ),
                sku=chosen.get("sku"), source=f"commitments:{chosen['_line']}",
            ))
        if len(versions) > len(latest):
            issues.append(Issue(
                code="commitment_version_superseded",
                severity="info",
                message=f"记录 {ident} 有较早版本，已按 {latest_day.isoformat()} 的最新版本计算",
                sku=chosen.get("sku"), source=f"commitments:{chosen['_line']}",
            ))
        merged.append(chosen)
    return merged


def normalize(snapshot: Snapshot, policy: Policy) -> Normalized:
    out = Normalized()
    out.notes.extend(snapshot.notes)
    out.as_of = policy.as_of
    commitment_rows = _dedupe_commitments(snapshot.commitments, out.issues)

    for row in snapshot.inventory:
        holding, issues = _holding_from_row(
            row, policy, status_column="status" in snapshot.inventory_columns,
        )
        out.issues.extend(issues)
        if holding is not None:
            out.holdings.append(holding)

    # 先给调拨确定状态，再据此判断接收方库存是否已经重复上报。
    transfers: list[Transfer] = []
    for row in commitment_rows:
        kind = _classify_kind(row.get("kind"))
        if kind != "transfer":
            continue
        state_raw = row.get("state")
        state = transfer_state(state_raw)
        transfer = Transfer(
            id=row["id"],
            sku=row["sku"],
            origin=row.get("from"),
            destination=row.get("to"),
            qty=row.get("qty"),
            state=state,
            state_raw=state_raw,
            day=row.get("day"),
        )
        transfers.append(transfer)
    out.transfers.extend(transfers)

    for row in commitment_rows:
        kind = _classify_kind(row.get("kind"))
        line = row["_line"]
        if kind == "unknown":
            out.issues.append(Issue(
                code="unknown_commitment_kind",
                severity="warning",
                message=f"commitment kind={row.get('kind')!r} 无法识别，未纳入任何口径",
                sku=row.get("sku"), source=f"commitments:{line}",
            ))
            continue
        if kind == "reservation":
            share_low, share_high = reservation_effect(row.get("state"))
            if row.get("state") is None:
                out.issues.append(Issue(
                    code="unconfirmed_reservation_state",
                    severity="warning",
                    message="订单预占状态缺报；可用量按可能被占用的 0 至全量给出范围",
                    sku=row["sku"], channel=row.get("from"),
                    quantity=None,
                    source=f"commitments:{line}",
                ))
            out.reservations.append(Reservation(
                id=row["id"],
                sku=row["sku"],
                location=row.get("from") or row.get("to"),
                qty=row.get("qty"),
                state_raw=row.get("state"),
                share_low=share_low,
                share_high=share_high,
                day=row.get("day"),
            ))
        elif kind == "transfer":
            continue
        elif kind == "marketing":
            state = marketing_state(row.get("state"))
            if state == "unknown":
                out.issues.append(Issue(
                    code="unknown_marketing_state",
                    severity="warning",
                    message=f"营销资源状态 {row.get('state')!r} 无法识别，按未确认可重排处理",
                    sku=row["sku"], channel=row.get("from") or row.get("to"),
                    source=f"commitments:{line}",
                ))
            out.marketing.append(MarketingResource(
                id=row["id"],
                sku=row["sku"],
                channel=row.get("from") or row.get("to"),
                qty=row.get("qty"),
                state=state,
                state_raw=row.get("state"),
                slot=row.get("slot") or row.get("day"),
                budget=row.get("budget"),
                min_stock=row.get("min_stock"),
                note=row.get("note"),
                line=line,
            ))

    for row in snapshot.events:
        kind = normalize_token(row.get("kind")) or ""
        out.events.append(dict(row, _kind=kind))

    out.sales = list(snapshot.sales)
    return out
