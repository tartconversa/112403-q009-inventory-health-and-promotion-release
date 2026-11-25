"""领域模型：数量范围、策略参数与规范化输入。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Range:
    """一个非负数量范围。

    ``high`` 为 ``None`` 表示来源缺报且无法给出上限；此时 ``low`` 是已确认
    数量，计划只能使用 ``low``。所有业务计算都保留范围，避免把未知当零，也
    避免把冲突的两份上报相加。
    """

    low: float = 0.0
    high: float | None = 0.0

    def __post_init__(self) -> None:
        if self.low < 0:
            object.__setattr__(self, "low", 0.0)
        if self.high is not None and self.high < self.low:
            object.__setattr__(self, "high", self.low)

    @classmethod
    def zero(cls) -> "Range":
        return cls(0.0, 0.0)

    @classmethod
    def unknown(cls) -> "Range":
        return cls(0.0, None)

    @classmethod
    def exact(cls, qty: float) -> "Range":
        return cls(qty, qty)

    @classmethod
    def between(cls, low: float, high: float | None) -> "Range":
        return cls(low, high)

    def __add__(self, other: "Range") -> "Range":
        high = None if self.high is None or other.high is None else self.high + other.high
        return Range(self.low + other.low, high)

    def __sub__(self, other: "Range") -> "Range":
        """self 减去 other。低位按对方最大可能值扣减，高位按最小可能值扣减。"""
        low = 0.0 if other.high is None else max(0.0, self.low - other.high)
        high = None if self.high is None else max(low, self.high - other.low)
        return Range(low, high)

    def __mul__(self, factor: float) -> "Range":
        if factor < 0:
            raise ValueError("Range 只能乘以非负数")
        high = None if self.high is None else self.high * factor
        return Range(self.low * factor, high)

    @property
    def is_exact(self) -> bool:
        return self.high is not None and abs(self.high - self.low) < 1e-9

    @property
    def is_unbounded(self) -> bool:
        return self.high is None

    @property
    def midpoint(self) -> float:
        if self.high is None:
            return self.low
        return (self.low + self.high) / 2.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "low": round(self.low + 0.0, 2),
            "high": None if self.high is None else round(self.high + 0.0, 2),
            "unbounded": self.high is None,
        }


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Issue:
    """一条需要人工确认或注意的事项，可携带数量范围。"""

    code: str
    severity: str
    message: str
    sku: str | None = None
    channel: str | None = None
    batch: str | None = None
    quantity: "Range | None" = None
    source: str | None = None
    assumption: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "sku": self.sku,
            "channel": self.channel,
            "batch": self.batch,
            "quantity": None if self.quantity is None else self.quantity.as_dict(),
            "source": self.source,
            "assumption": self.assumption,
        }

    def sort_key(self) -> tuple:
        return (
            SEVERITY_ORDER.get(self.severity, 9),
            self.sku or "",
            self.channel or "",
            self.code,
            self.message,
        )


SALEABLE_STATES = {
    "saleable", "available", "ok", "normal", "good", "可售", "正常", "在库", "良品",
}
BLOCKED_STATES = {
    "blocked", "quarantine", "hold", "damaged", "unsaleable", "unavailable",
    "frozen", "scrap", "冻结", "待检", "不可售", "残损", "封存", "隔离", "报废",
}
UNCONFIRMED_STATES = {
    "pending", "unconfirmed", "review", "check", "unknown", "待确认", "未确认",
    "待核", "未知", "待定", "待定状态",
}
CANCELLED_STATES = {
    "cancelled", "canceled", "rejected", "withdrawn", "void",
    "已取消", "已拒绝", "已作废",
}
CONFIRMED_STATES = {
    "confirmed", "signed", "approved", "locked", "booked", "contracted",
    "已确认", "已签", "已锁定", "已签约", "已对外确认",
}
PROPOSED_STATES = {
    "proposed", "draft", "planned", "tentative", "new", "提议", "计划", "草稿",
    "待确认",
}
RELEASED_STATES = {
    "released", "closed", "fulfilled", "completed", "expired", "cancelled",
    "已释放", "已完成", "已关闭",
}
OPEN_RESERVATION_STATES = {
    "open", "active", "reserved", "new", "占用", "开放", "有效", "待处理",
}
IN_TRANSIT_STATES = {
    "in-transit", "in_transit", "intransit", "dispatched", "shipped", "sent",
    "在途", "已发出", "运输中",
}
RECEIVED_STATES = {
    "received", "signed", "delivered", "arrived", "签收", "已收货", "已到达",
}
PLANNED_TRANSFER_STATES = {
    "planned", "draft", "requested", "待发", "计划", "申请",
}


def normalize_token(raw: str | None) -> str | None:
    if raw is None:
        return None
    return raw.strip().lower().replace(" ", "")


def status_class(raw: str | None) -> tuple[str, str | None]:
    """返回 ``(saleable|blocked|unconfirmed, 无法识别的原值)``。"""
    token = normalize_token(raw)
    if token is None:
        return "unconfirmed", None
    if token in {t.replace(" ", "") for t in SALEABLE_STATES}:
        return "saleable", None
    if token in {t.replace(" ", "") for t in BLOCKED_STATES}:
        return "blocked", None
    return "unconfirmed", (None if token in {t.replace(" ", "") for t in UNCONFIRMED_STATES} else raw)


def reservation_effect(raw: str | None) -> tuple[float, float]:
    """返回预占状态对应的 ``(最少占用比例, 最多占用比例)``。

    状态明确为开放时 100% 占用；已关闭/取消/释放时不占用；状态缺失或无法
    识别时可能占用也可能不占用，因此形成 ``[0, qty]`` 的范围。
    """
    token = normalize_token(raw)
    if token is None:
        return 0.0, 1.0
    if token in {t.replace(" ", "") for t in OPEN_RESERVATION_STATES}:
        return 1.0, 1.0
    if token in {t.replace(" ", "") for t in RELEASED_STATES}:
        return 0.0, 0.0
    return 0.0, 1.0


def transfer_state(raw: str | None) -> str:
    """返回 ``in-transit|received|planned|cancelled|unknown``。"""
    token = normalize_token(raw)
    if token is None:
        return "unknown"
    if token in {t.replace(" ", "") for t in IN_TRANSIT_STATES}:
        return "in-transit"
    if token in {t.replace(" ", "") for t in RECEIVED_STATES}:
        return "received"
    if token in {t.replace(" ", "") for t in CANCELLED_STATES}:
        return "cancelled"
    if token in {t.replace(" ", "") for t in PLANNED_TRANSFER_STATES}:
        return "planned"
    return "unknown"


def marketing_state(raw: str | None) -> str:
    """返回 ``confirmed|proposed|cancelled|unknown``。"""
    token = normalize_token(raw)
    if token is None:
        return "unknown"
    if token in {t.replace(" ", "") for t in CONFIRMED_STATES}:
        return "confirmed"
    if token in {t.replace(" ", "") for t in CANCELLED_STATES}:
        return "cancelled"
    if token in {t.replace(" ", "") for t in PROPOSED_STATES}:
        return "proposed"
    return "unknown"


def channel_kind(location: str) -> str:
    """按常见前缀给渠道分类，仅用于展示。"""
    prefix = location.strip().upper()[:2]
    if prefix in {"WH", "DC", "FC"}:
        return "仓库"
    if prefix in {"ST", "SH", "SO"}:
        return "门店"
    if prefix in {"EC", "ON", "WE", "TM", "JD"}:
        return "电商"
    return "其他"


@dataclass(frozen=True)
class Policy:
    """财务、供应链与商品团队共同确认的约束。"""

    as_of: date | None = None
    near_expiry_days: int = 60
    safety_cover_days: float = 21.0
    overstock_cover_days: float = 90.0
    min_cover_days: float = 14.0
    stale_snapshot_days: int = 3
    default_inventory_status: str = "saleable"
    budget_total: float | None = None
    min_stock: dict[str, dict[str, float]] = field(default_factory=dict)
    min_stock_sku: dict[str, float] = field(default_factory=dict)
    channel_labels: dict[str, str] = field(default_factory=dict)

    def floor_for(self, sku: str, location: str) -> float:
        return float(
            self.min_stock.get(location, {}).get(sku, self.min_stock_sku.get(sku, 0.0))
        )

    def label_for(self, location: str) -> str:
        return self.channel_labels.get(location) or f"{channel_kind(location)}·{location}"


@dataclass(frozen=True)
class Holding:
    """规范化后的批次库存观测。"""

    location: str
    sku: str
    batch: str
    qty: float | None
    status_raw: str | None
    status: str
    unrecognized_status: str | None
    expiry: date | None
    received: date | None
    day: date
    substitute_for: str | None
    status_supplied: bool
    line: int
    revision_id: str | None = None
    note: str | None = None
    saleable_ratio_low: float = 1.0
    saleable_ratio_high: float | None = 1.0


@dataclass(frozen=True)
class Reservation:
    id: str
    sku: str
    location: str | None
    qty: float | None
    state_raw: str | None
    share_low: float
    share_high: float
    day: date | None


@dataclass(frozen=True)
class Transfer:
    id: str
    sku: str
    origin: str | None
    destination: str | None
    qty: float | None
    state: str
    state_raw: str | None
    day: date | None
    already_reported: bool | None = None


@dataclass(frozen=True)
class MarketingResource:
    id: str
    sku: str
    channel: str | None
    qty: float | None
    state: str
    state_raw: str | None
    slot: date | None
    budget: float | None
    min_stock: float | None
    note: str | None
    line: int

    @property
    def protected(self) -> bool:
        return self.state == "confirmed"
