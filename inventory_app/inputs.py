"""输入读取与校验。

所有输入都是 CSV。空单元格表示“来源未提供该值”，因此解析后保留为 ``None``，
由引擎决定这是缺报、未知状态还是可忽略字段。读取器只做结构校验，不做业务
判断；业务口径在 :mod:`inventory_app.normalize` 中集中处理。

样例文件只包含核心列；以下可选列可按需追加：

* ``inventory.csv``: ``status``、``received``、``substitute_for``、``note``
* ``commitments.csv``: ``day``、``slot``、``budget``、``min_stock``、``note``
* ``sales.csv``: 无扩展列

库存和销售文件必须存在；commitments 文件可以缺省（视为没有任何预占、调拨
或营销计划）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

INVENTORY_COLUMNS = [
    "location", "sku", "batch", "qty", "expiry", "day",
    "status", "received", "substitute_for", "note",
]
COMMITMENT_COLUMNS = [
    "id", "sku", "from", "to", "qty", "kind", "state",
    "day", "slot", "budget", "min_stock", "note",
]
SALES_COLUMNS = ["sku", "location", "units", "returns", "spend", "period"]
EVENT_COLUMNS = [
    "id", "sku", "kind", "qty", "location", "batch", "day",
    "state", "target", "note",
]

REQUIRED_INVENTORY = ["location", "sku", "batch", "qty", "expiry", "day"]
REQUIRED_COMMITMENTS = ["id", "sku", "from", "to", "qty", "kind", "state"]
REQUIRED_SALES = ["sku", "location", "units", "returns", "spend", "period"]
REQUIRED_EVENTS = ["kind", "day"]

MISSING_TOKENS = {
    "", "na", "n/a", "nan", "none", "null", "unknown", "missing", "?",
    "未知", "缺报", "无",
}


class InputError(ValueError):
    """输入结构或取值无法解释时抛出，消息面向人工核查。"""


@dataclass(frozen=True)
class Snapshot:
    """一次读取得到的原始行集合，保留行号以便追溯。"""

    inventory: list[dict[str, Any]] = field(default_factory=list)
    commitments: list[dict[str, Any]] = field(default_factory=list)
    sales: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    inventory_columns: frozenset[str] = frozenset()

    def extend(self, other: "Snapshot") -> "Snapshot":
        return Snapshot(
            inventory=self.inventory + other.inventory,
            commitments=self.commitments + other.commitments,
            sales=self.sales + other.sales,
            events=self.events + other.events,
            notes=self.notes + other.notes,
            inventory_columns=self.inventory_columns | other.inventory_columns,
        )


def _is_missing(value: str | None) -> bool:
    return value is None or str(value).strip().lower() in MISSING_TOKENS


def _clean(headers: list[str], row: dict[str, Any], line: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in headers:
        raw = row.get(col)
        out[col] = None if _is_missing(raw) else str(raw).strip()
    out["_line"] = line
    return out


def _number(
    value: str | None, *, what: str, source: str, line: int,
    allow_negative: bool = False, optional: bool = False,
) -> float | None:
    if value is None:
        if optional:
            return None
        raise InputError(f"{source}:{line} 缺少 {what}；请补报或标注为未知后再确认")
    try:
        number = float(value.replace(",", ""))
    except ValueError as exc:
        raise InputError(f"{source}:{line} 的 {what}={value!r} 不是数字") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise InputError(f"{source}:{line} 的 {what}={value!r} 不是有限数字")
    if not allow_negative and number < 0:
        raise InputError(f"{source}:{line} 的 {what}={value!r} 不能为负")
    return number


def _date(
    value: str | None, *, what: str, source: str, line: int, optional: bool = False,
) -> date | None:
    if value is None:
        if optional:
            return None
        raise InputError(f"{source}:{line} 缺少 {what}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InputError(f"{source}:{line} 的 {what}={value!r} 不是 ISO 日期（YYYY-MM-DD）") from exc


def _read_rows(path: Path, required: list[str], source: str) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        raise InputError(f"找不到输入文件：{path}")
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames is None:
            raise InputError(f"{source}: 文件为空，缺少表头")
        headers = [h.strip() for h in reader.fieldnames if h and h.strip()]
        missing = [col for col in required if col not in headers]
        if missing:
            raise InputError(f"{source}: 缺少列 {missing}；实际列 {headers}")
        rows: list[dict[str, Any]] = []
        for line, raw in enumerate(reader, start=2):
            if raw.get(None) is not None:
                raise InputError(f"{source}:{line} 的列数多于表头，无法确定归属")
            rows.append(_clean(headers, raw, line))
    return rows, headers


def _known_columns(headers: list[str], columns: list[str]) -> set[str]:
    return {h for h in headers if h in columns and h not in REQUIRED_SALES}


def read_inventory(path: str | Path, source: str = "inventory") -> Snapshot:
    rows, headers = _read_rows(Path(path), REQUIRED_INVENTORY, source)
    parsed = []
    for row in rows:
        line = row["_line"]
        if not row["location"] or not row["sku"] or not row["batch"]:
            raise InputError(f"{source}:{line} 的 location/sku/batch 不能为空")
        row["qty"] = _number(
            row["qty"], what="qty", source=source, line=line,
            allow_negative=True, optional=True,
        )
        row["expiry"] = _date(
            row["expiry"], what="expiry", source=source, line=line, optional=True,
        )
        row["day"] = _date(row["day"], what="day", source=source, line=line)
        row["received"] = _date(
            row.get("received"), what="received", source=source, line=line, optional=True,
        )
        parsed.append(row)
    return Snapshot(inventory=parsed, inventory_columns=frozenset(_known_columns(headers, INVENTORY_COLUMNS)))


def read_commitments(path: str | Path, source: str = "commitments") -> Snapshot:
    rows, _ = _read_rows(Path(path), REQUIRED_COMMITMENTS, source)
    parsed = []
    for row in rows:
        line = row["_line"]
        if not row["id"]:
            raise InputError(f"{source}:{line} 缺少 id")
        if not row["sku"]:
            raise InputError(f"{source}:{line} 缺少 sku")
        if not row["kind"]:
            raise InputError(f"{source}:{line} 缺少 kind")
        row["qty"] = _number(
            row["qty"], what="qty", source=source, line=line,
            allow_negative=True, optional=True,
        )
        for col in ("day", "slot"):
            row[col] = _date(
                row.get(col), what=col, source=source, line=line, optional=True,
            )
        for col in ("budget", "min_stock"):
            row[col] = _number(
                row.get(col), what=col, source=source, line=line, optional=True,
            )
        parsed.append(row)
    return Snapshot(commitments=parsed)


def read_sales(path: str | Path, source: str = "sales") -> Snapshot:
    rows, _ = _read_rows(Path(path), REQUIRED_SALES, source)
    parsed = []
    for row in rows:
        line = row["_line"]
        if not row["sku"] or not row["location"] or not row["period"]:
            raise InputError(f"{source}:{line} 的 sku/location/period 不能为空")
        row["units"] = _number(row["units"], what="units", source=source, line=line)
        row["returns"] = _number(row["returns"], what="returns", source=source, line=line)
        row["spend"] = _number(
            row["spend"], what="spend", source=source, line=line, optional=True,
        ) or 0.0
        parsed.append(row)
    return Snapshot(sales=parsed)


def read_events(path: str | Path, source: str = "events") -> Snapshot:
    rows, _ = _read_rows(Path(path), REQUIRED_EVENTS, source)
    parsed = []
    for row in rows:
        line = row["_line"]
        if not row["kind"]:
            raise InputError(f"{source}:{line} 缺少 kind")
        row["qty"] = _number(
            row["qty"], what="qty", source=source, line=line,
            allow_negative=True, optional=True,
        )
        row["day"] = _date(row["day"], what="day", source=source, line=line)
        row["id"] = row["id"] or f"E{line - 1}"
        parsed.append(row)
    return Snapshot(events=parsed)


def load_directory(directory: str | Path, *, with_commitments: bool = True) -> Snapshot:
    """读取目录下的 inventory.csv、commitments.csv、sales.csv。"""
    base = Path(directory)
    snapshot = read_inventory(base / "inventory.csv")
    if with_commitments and (base / "commitments.csv").exists():
        snapshot = snapshot.extend(read_commitments(base / "commitments.csv"))
    else:
        snapshot = snapshot.extend(Snapshot(notes=["未提供 commitments.csv，按无预占、调拨或营销计划处理"]))
    return snapshot.extend(read_sales(base / "sales.csv"))
