"""库存可用量、预占与调拨的合并口径。

这里集中实现三个最容易出错的规则：

1. **同批次多份上报不相加。** 同一 ``(location, sku, batch)`` 只取最新日期
   的一份；同一天多份内容不同的上报合并成范围 ``[最小值, 最大值]``。不同日期的
   旧快照保留为历史，不参与当前可用量。
2. **订单预占只扣减。** 预占数量永远不进入可售量；状态未知时按 ``[0, qty]``
   扣减，因此可用量同时给出“最乐观”和“最保守”两端。
3. **调拨不重复计入。** 在途调拨不计入即时可用量。若无法确认接收方报表是否
   已包含该批货（``already_reported`` 未知），接收方一侧按可能重复计入给出
   ``[0, qty]`` 的扣减范围；发出方一侧同样给出 ``[0, qty]`` 的“可能尚未扣减”
   范围。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from .model import (
    Holding,
    Issue,
    Range,
    Reservation,
    Transfer,
)


@dataclass
class BatchSupply:
    """一个批次当前可确认的库存范围。"""

    location: str
    sku: str
    batch: str
    expiry: date | None
    received: date | None
    observed: Range
    saleable: Range
    blocked: Range
    status: str
    status_raw: str | None
    sources: list[int] = field(default_factory=list)
    substitute_for: str | None = None


def merge_holdings(
    holdings: Sequence[Holding],
) -> tuple[dict[tuple[str, str], list[BatchSupply]], list[Issue]]:
    """按批次合并观测，返回 ``(location, sku) -> 批次列表`` 与冲突事项。"""
    issues: list[Issue] = []
    grouped: dict[tuple[str, str, str], list[Holding]] = defaultdict(list)
    for holding in holdings:
        grouped[(holding.location, holding.sku, holding.batch)].append(holding)

    supply: dict[tuple[str, str], list[BatchSupply]] = defaultdict(list)
    for (location, sku, batch), observations in sorted(grouped.items()):
        latest = max(o.day for o in observations)
        current = [o for o in observations if o.day == latest]
        stale = [o for o in observations if o.day != latest]
        if stale:
            issues.append(Issue(
                code="stale_snapshot_ignored",
                severity="info",
                message=(
                    f"该批次有 {len(stale)} 条早于 {latest.isoformat()} 的上报，"
                    "已按最新快照计算；历史快照不叠加"
                ),
                sku=sku, channel=location, batch=batch,
                source="inventory",
            ))

        known = [o for o in current if o.qty is not None]
        unknown_qty = [o for o in current if o.qty is None]
        if known:
            low = min(o.qty for o in known)  # type: ignore[arg-type]
            high: float | None = max(o.qty for o in known)  # type: ignore[arg-type]
            if unknown_qty:
                high = None
            if len({o.qty for o in known}) > 1:
                issues.append(Issue(
                    code="conflicting_snapshot_qty",
                    severity="warning",
                    message=(
                        f"同一天有 {len(current)} 份不一致的库存上报，"
                        f"已确认范围 {low:g}–{high:g}，未相加"
                    ),
                    sku=sku, channel=location, batch=batch,
                    quantity=Range(low, high),
                    source="inventory",
                ))
        else:
            low, high = 0.0, None
            issues.append(Issue(
                code="missing_inventory_qty",
                severity="warning",
                message="该批次仅有无数量的上报；已确认数量为 0，上限未知",
                sku=sku, channel=location, batch=batch,
                quantity=Range.unknown(),
                source="inventory",
            ))

        statuses = {o.status for o in current}
        status_raw = current[0].status_raw
        if len(statuses) > 1:
            issues.append(Issue(
                code="conflicting_inventory_status",
                severity="warning",
                message=(
                    f"同一批次的状态在 {'/'.join(sorted(statuses))} 之间冲突；"
                    "按最保守端扣减可售量"
                ),
                sku=sku, channel=location, batch=batch,
                source="inventory",
            ))
            saleable_low_ratio = min(o.saleable_ratio_low for o in current)
            saleable_high_ratio = min(
                (o.saleable_ratio_high for o in current if o.saleable_ratio_high is not None),
                default=0.0,
            )
        else:
            saleable_low_ratio = current[0].saleable_ratio_low
            saleable_high_ratio = current[0].saleable_ratio_high

        observed = Range(low, high)
        saleable = Range(
            low * saleable_low_ratio,
            None if high is None or saleable_high_ratio is None else high * saleable_high_ratio,
        )
        blocked = observed - saleable
        if saleable_high_ratio is None:
            saleable = Range(low * saleable_low_ratio, None)

        supply[(location, sku)].append(BatchSupply(
            location=location,
            sku=sku,
            batch=batch,
            expiry=current[0].expiry,
            received=current[0].received,
            observed=observed,
            saleable=saleable,
            blocked=blocked,
            status="saleable" if statuses == {"saleable"} else (
                "blocked" if statuses == {"blocked"} else "unconfirmed"
            ),
            status_raw=status_raw,
            sources=[o.line for o in observations],
            substitute_for=current[0].substitute_for,
        ))
    return supply, issues


@dataclass
class ReservationImpact:
    """某渠道某 SKU 被预占扣减的范围，分已确认与可能两部分。"""

    confirmed: Range = field(default_factory=Range.zero)
    possible: Range = field(default_factory=Range.zero)

    @property
    def total_low(self) -> float:
        return self.confirmed.low + self.possible.low

    @property
    def total_high(self) -> float | None:
        if self.confirmed.high is None or self.possible.high is None:
            return None
        return self.confirmed.high + self.possible.high

    def merged(self) -> Range:
        return Range(self.total_low, self.total_high)


def reservation_impacts(
    reservations: Sequence[Reservation],
    known_locations: set[tuple[str, str]],
) -> tuple[dict[tuple[str, str], ReservationImpact], list[Issue]]:
    issues: list[Issue] = []
    impacts: dict[tuple[str, str], ReservationImpact] = defaultdict(ReservationImpact)
    for reservation in reservations:
        if reservation.location is None:
            issues.append(Issue(
                code="reservation_without_location",
                severity="warning",
                message=f"订单预占 {reservation.id} 未指明渠道，无法对应到任何库存；未扣减任何渠道",
                sku=reservation.sku,
                source=f"reservation:{reservation.id}",
            ))
            continue
        key = (reservation.location, reservation.sku)
        if reservation.qty is None:
            issues.append(Issue(
                code="reservation_without_qty",
                severity="warning",
                message=f"订单预占 {reservation.id} 缺少数量，按未知占用处理",
                sku=reservation.sku, channel=reservation.location,
                quantity=Range.unknown(), source=f"reservation:{reservation.id}",
            ))
            impacts[key].possible = impacts[key].possible + Range.unknown()
            continue
        if reservation.qty < 0:
            issues.append(Issue(
                code="negative_reservation",
                severity="warning",
                message=f"订单预占 {reservation.id} 数量为负（{reservation.qty:g}），按 0 处理并请订单侧确认",
                sku=reservation.sku, channel=reservation.location,
                source=f"reservation:{reservation.id}",
            ))
            continue
        if (reservation.location, reservation.sku) not in known_locations:
            issues.append(Issue(
                code="reservation_without_inventory",
                severity="warning",
                message=(
                    f"订单预占 {reservation.id} 指向未有库存快照的渠道 {reservation.location}；"
                    "不为该渠道推算可用量，请补报库存"
                ),
                sku=reservation.sku, channel=reservation.location,
                source=f"reservation:{reservation.id}",
            ))
            continue
        exact = Range.exact(reservation.qty)
        if reservation.share_low >= 1.0 and reservation.share_high >= 1.0:
            impacts[key].confirmed = impacts[key].confirmed + exact
        elif reservation.share_low <= 0.0 and reservation.share_high <= 0.0:
            continue
        else:
            impacts[key].possible = impacts[key].possible + Range(
                exact.low * reservation.share_low,
                None if exact.high is None else exact.high * reservation.share_high,
            )
            issues.append(Issue(
                code="unconfirmed_reservation_state",
                severity="warning",
                message=(
                    f"订单预占 {reservation.id} 的状态 {reservation.state_raw!r} 不能确认仍有效；"
                    f"可用量按扣除 0–{reservation.qty:g} 给出范围"
                ),
                sku=reservation.sku, channel=reservation.location,
                quantity=Range(0.0, reservation.qty),
                source=f"reservation:{reservation.id}",
            ))
    return impacts, issues


@dataclass
class TransferImpact:
    # 发出方：报表可能尚未扣减的在途数量，按 [0, qty] 扣减。
    origin_uncertain: Range = field(default_factory=Range.zero)
    # 接收方：报表可能已提前计入的在途数量，按 [0, qty] 扣减。
    destination_possible: Range = field(default_factory=Range.zero)


def transfer_impacts(
    transfers: Sequence[Transfer],
    inventory_locations: set[tuple[str, str]],
) -> tuple[dict[tuple[str, str], TransferImpact], dict[tuple[str, str], TransferImpact], list[Issue]]:
    """返回 ``(发出方影响, 接收方影响, 事项)``。

    发出方影响是“可能尚未从发出方扣减的数量”，接收方影响是“可能已经计入
    接收方报表的数量”。两者都只用于形成保守端，不会被加成新的可用库存。
    """
    issues: list[Issue] = []
    origin_side: dict[tuple[str, str], TransferImpact] = defaultdict(TransferImpact)
    destination_side: dict[tuple[str, str], TransferImpact] = defaultdict(TransferImpact)

    for transfer in transfers:
        if transfer.qty is None:
            issues.append(Issue(
                code="transfer_without_qty",
                severity="warning",
                message=f"调拨 {transfer.id} 缺少数量，无法判断在途规模",
                sku=transfer.sku, channel=transfer.destination or transfer.origin,
                source=f"transfer:{transfer.id}",
            ))
            continue
        if transfer.qty < 0:
            issues.append(Issue(
                code="negative_transfer",
                severity="warning",
                message=f"调拨 {transfer.id} 数量为负（{transfer.qty:g}），按 0 处理",
                sku=transfer.sku, source=f"transfer:{transfer.id}",
            ))
            continue
        amount = Range.exact(transfer.qty)
        if transfer.state == "cancelled":
            continue

        origin = transfer.origin
        destination = transfer.destination
        origin_key = (origin, transfer.sku) if origin else None
        destination_key = (destination, transfer.sku) if destination else None

        if transfer.state == "received":
            # 已签收：接收方快照理应已包含，不再额外扣减；若发出方仍有货未扣，
            # 由盘点差异事件而不是调拨记录来修正。
            continue

        if transfer.state == "in-transit":
            # 两侧都按“最多可能重复计入 qty 件”的 [0, qty] 区间扣减：
            # 报表可能已扣/已计，也可能没有。
            at_risk = Range(0.0, transfer.qty)
            if origin_key and origin_key in inventory_locations:
                # 在途已经离开发出方，但报表可能尚未扣减 -> 发出方可用量可能被高估。
                origin_side[origin_key].origin_uncertain = origin_side[origin_key].origin_uncertain + at_risk
                issues.append(Issue(
                    code="in_transit_may_still_be_in_origin",
                    severity="warning",
                    message=(
                        f"调拨 {transfer.id} 在途 {transfer.qty:g} 件；"
                        "若发出方报表尚未扣减，发出方可用量会被高估，已给出扣减范围"
                    ),
                    sku=transfer.sku, channel=origin,
                    quantity=Range(0.0, transfer.qty),
                    source=f"transfer:{transfer.id}",
                ))
            if destination_key and destination_key in inventory_locations:
                destination_side[destination_key].destination_possible = (
                    destination_side[destination_key].destination_possible + at_risk
                )
                issues.append(Issue(
                    code="in_transit_uncertain_ownership",
                    severity="warning",
                    message=(
                        f"调拨 {transfer.id} 在途 {transfer.qty:g} 件；"
                        "接收方报表若已提前计入需扣回，未计入则不能作为即时可用，已给出范围"
                    ),
                    sku=transfer.sku, channel=destination,
                    quantity=Range(0.0, transfer.qty),
                    source=f"transfer:{transfer.id}",
                ))
            elif destination_key and origin is not None:
                # 仓库不单独按渠道核算需求，但接收方门店必须如实记录在途。
                issues.append(Issue(
                    code="transfer_destination_without_inventory",
                    severity="info",
                    message=(
                        f"调拨 {transfer.id} 的接收方 {destination} 无库存快照；"
                        "在途货不作为该渠道的即时可用量"
                    ),
                    sku=transfer.sku, channel=destination,
                    source=f"transfer:{transfer.id}",
                ))
        elif transfer.state == "planned":
            issues.append(Issue(
                code="planned_transfer_not_committed",
                severity="info",
                message=(
                    f"调拨 {transfer.id} 尚为计划状态（{transfer.qty:g} 件），"
                    "未计入任何渠道可用量"
                ),
                sku=transfer.sku, channel=destination or origin,
                source=f"transfer:{transfer.id}",
            ))
        else:
            # 状态未知：两侧都可能已计入或未计入，给出范围。
            at_risk = Range(0.0, transfer.qty)
            if origin_key and origin_key in inventory_locations:
                origin_side[origin_key].origin_uncertain = (
                    origin_side[origin_key].origin_uncertain + at_risk
                )
            if destination_key and destination_key in inventory_locations:
                destination_side[destination_key].destination_possible = (
                    destination_side[destination_key].destination_possible + at_risk
                )
            issues.append(Issue(
                code="unknown_transfer_ownership",
                severity="warning",
                message=(
                    f"调拨 {transfer.id} 状态 {transfer.state_raw!r} 无法确认归属；"
                    "发出方与接收方都按可能重复计入给出扣减范围"
                ),
                sku=transfer.sku, channel=destination or origin,
                quantity=Range(0.0, transfer.qty),
                source=f"transfer:{transfer.id}",
            ))
    return origin_side, destination_side, issues


@dataclass
class VenueSupply:
    """一个渠道某 SKU 的可用量分解。"""

    location: str
    sku: str
    batches: list[BatchSupply] = field(default_factory=list)
    gross: Range = field(default_factory=Range.zero)
    saleable_gross: Range = field(default_factory=Range.zero)
    blocked: Range = field(default_factory=Range.zero)
    reserved: Range = field(default_factory=Range.zero)
    origin_uncertain: Range = field(default_factory=Range.zero)
    destination_possible: Range = field(default_factory=Range.zero)
    status_unconfirmed: bool = False
    missing_snapshot: bool = False

    @property
    def sellable(self) -> Range:
        """可确认的可售数量范围（已扣除预占与在途重复风险）。"""
        net = self.saleable_gross - self.reserved - self.origin_uncertain
        net = net - self.destination_possible
        return net


def build_supply(
    supply_by_venue: dict[tuple[str, str], list[BatchSupply]],
    reservations: dict[tuple[str, str], ReservationImpact],
    origin_side: dict[tuple[str, str], TransferImpact],
    destination_side: dict[tuple[str, str], TransferImpact],
    keys: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], VenueSupply]:
    venues: dict[tuple[str, str], VenueSupply] = {}
    for location, sku in keys:
        batches = sorted(
            supply_by_venue.get((location, sku), []),
            key=lambda b: (b.expiry or date.max, b.batch),
        )
        venue = VenueSupply(location=location, sku=sku, batches=batches)
        for batch in batches:
            venue.gross = venue.gross + batch.observed
            venue.saleable_gross = venue.saleable_gross + batch.saleable
            venue.blocked = venue.blocked + batch.blocked
        impact = reservations.get((location, sku))
        if impact is not None:
            venue.reserved = impact.merged()
        if (location, sku) in origin_side:
            venue.origin_uncertain = origin_side[(location, sku)].origin_uncertain
        if (location, sku) in destination_side:
            venue.destination_possible = destination_side[(location, sku)].destination_possible
        venue.status_unconfirmed = any(
            b.status == "unconfirmed" for b in batches
        )
        venues[(location, sku)] = venue
    return venues
