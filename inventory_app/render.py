"""终端输出：把评审结果渲染成便于资源评审会使用的文本与 JSON。"""

from __future__ import annotations

import json
from typing import Any

from .engine import Review
from .marketing import PRIORITY_LABELS


def _rng(value: dict[str, Any] | None) -> str:
    if value is None:
        return "—"
    low = value["low"]
    if value["unbounded"]:
        return f"≥{low:g} 件（上限未知）"
    high = value["high"]
    if abs(high - low) < 1e-9:
        return f"{low:g} 件"
    return f"{low:g}–{high:g} 件"


def _days(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f} 天"


def _cover(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f} 天"


def _width(text: str) -> int:
    """终端显示宽度：CJK 字符按 2 列计。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _width(text))
    if align == "right":
        return " " * gap + text + "  "
    return text + " " * gap + "  "


SEVERITY_LABELS = {"critical": "严重", "warning": "注意", "info": "提示"}
RISK_LABELS = {
    "shortage": "缺货风险",
    "overstock": "积压风险",
    "balanced": "平衡",
    "unknown": "待确认",
}
SIGNAL_LABELS = {
    "fast": "畅销",
    "slow": "滞销",
    "healthy": "健康",
    "near_expiry": "临期",
    "near_expiry_tight": "临期偏紧",
    "near_expiry_overstock": "临期滞销",
    "no_demand": "无净需求",
    "missing": "缺需求数据",
    "unknown": "待确认",
}
STATUS_LABELS = {
    "protected": "已确认·保留",
    "manual": "转人工",
    "proposed": "未确认",
    "released": "已释放",
    "unreviewed": "未评审",
}


def render_text(review: Review) -> str:
    lines: list[str] = []
    add = lines.append
    counts = review.counts
    add("=" * 78)
    add(f"库存健康与营销资源评审　评审日 {review.as_of.isoformat()}")
    add("=" * 78)
    add(
        f"输入：{counts['holdings']} 条库存观测 · {counts['reservations']} 条订单预占 · "
        f"{counts['transfers']} 条调拨 · {counts['marketing_resources']} 条营销资源"
    )
    add(
        f"事项：严重 {counts['issues_critical']} · 注意 {counts['issues_warning']} · "
        f"提示 {sum(1 for i in review.issues if i.severity == 'info')}"
    )
    if review.plan.budget_unverified:
        add("预算未经校验：未提供预算上限或单位促销成本。")
    add("")

    add("— 渠道库存与需求 —")
    columns = [
        ("渠道", 14, "left"), ("商品", 7, "left"), ("在库", 16, "right"),
        ("可确认可售", 20, "right"), ("覆盖率", 14, "right"),
        ("库存龄", 12, "right"), ("健康信号", 12, "left"), ("风险", 10, "left"),
    ]
    add("".join(_pad(name, width, align) for name, width, align in columns))
    for report in review.reports:
        cells = [
            (report.label, 14, "left"), (report.sku, 7, "left"),
            (_rng(report.gross.as_dict()), 16, "right"),
            (_rng(report.sellable.as_dict()), 20, "right"),
            (_cover(report.cover_low), 14, "right"),
            (_days(report.age_days), 12, "right"),
            (SIGNAL_LABELS.get(report.signal, report.signal), 12, "left"),
            (RISK_LABELS.get(report.risk, report.risk), 10, "left"),
        ]
        add("".join(_pad(text, width, align) for text, width, align in cells))
    add("")
    add("  注：可确认可售 = 在库 − 封锁/过期 − 订单预占 − 在途可能重复计入；")
    add("      上限“未知”表示来源缺报或状态未确认，计划只按低端使用。")
    add("")

    add("— 商品级渠道张力 —")
    if not any(p.tension != "none" for p in review.portfolio):
        add("  未发现同一商品在不同渠道间缺货与积压并存的情况。")
    for portfolio in review.portfolio:
        if portfolio.tension == "none":
            continue
        tension_label = {
            "mixed": "同商品跨渠道方向相反",
            "shortage": "整体偏紧",
            "overstock": "整体积压",
        }.get(portfolio.tension, portfolio.tension)
        add(f"  {portfolio.sku}：{tension_label}")
        for detail in portfolio.tension_detail:
            add(f"    · {detail['message']}")
    add("")

    add("— 营销资源分配 —")
    plan = review.plan
    budget = "未提供上限" if plan.budget_total is None else f"{plan.budget_total:g}"
    add(
        f"  预算：上限 {budget} · 已确认 {plan.budget_committed:g} · "
        f"拟投放 {plan.budget_proposed:g} · 调整/释放 {plan.released} 条"
    )
    if plan.allocations:
        alloc_columns = [
            ("编号", 10, "left"), ("商品", 7, "left"), ("渠道", 10, "left"),
            ("申请", 7, "right"), ("建议", 7, "right"),
            ("状态", 14, "left"), ("优先级", 12, "left"),
        ]
        add("  " + "".join(_pad(name, width, align) for name, width, align in alloc_columns) + "说明")
        for allocation in plan.allocations:
            priority = "" if allocation.priority is None else PRIORITY_LABELS.get(
                allocation.priority, str(allocation.priority)
            )
            approved = "—" if allocation.approved is None else f"{allocation.approved:g}"
            cells = [
                (allocation.resource_id, 10, "left"), (allocation.sku, 7, "left"),
                (allocation.channel or "—", 10, "left"),
                (f"{allocation.requested:g}", 7, "right"), (approved, 7, "right"),
                (STATUS_LABELS.get(allocation.status, allocation.status), 14, "left"),
                (priority, 12, "left"),
            ]
            add("  " + "".join(_pad(text, width, align) for text, width, align in cells) + (allocation.reason or ""))
    for tension in plan.tensions:
        if tension.risk == "transfer_opportunity":
            add(f"  ⚠ {tension.message}")
    add("")

    manual = review.manual_items
    add("— 需人工处理的已确认安排 —")
    if not manual:
        add("  无。所有已确认资源均可按原决策执行。")
    for item in manual:
        shortfall = f"，缺口 {item['shortfall']:g} 件" if item["shortfall"] else ""
        add(
            f"  · {item['id']} {item['sku']} @ {item['channel'] or '未指定渠道'}："
            f"{item['conflict']}{shortfall}；保留原决策"
        )
    add("")

    add("— 待确认事项 —")
    if not review.issues:
        add("  无。")
    shown = 0
    for issue in sorted(review.issues, key=lambda i: i.sort_key()):
        if issue.severity == "info" and shown >= 40:
            continue
        where = " ".join(part for part in (issue.channel, issue.sku, issue.batch) if part)
        amount = "" if issue.quantity is None else f"［{_rng(issue.quantity.as_dict())}］"
        add(
            f"  [{SEVERITY_LABELS.get(issue.severity, issue.severity)}] "
            f"{issue.code}{(' ' + where) if where else ''}：{issue.message}{amount}"
        )
        shown += 1
    add("")
    return "\n".join(lines)


def render_json(review: Review) -> str:
    return json.dumps(review.to_dict(), ensure_ascii=False, indent=2)


def render_revision_text(revision) -> str:
    """一次事件修订后的前后对比。"""
    lines: list[str] = ["— 事件修订对比 —"]
    for event in revision.events:
        lines.append(
            f"  应用 {event.get('id')}（{event.get('kind')}）"
            f" {event.get('location') or ''} {event.get('sku') or ''}"
        )
    changed = [c for c in revision.changes if c.significant]
    if not changed:
        lines.append("  库存风险与可售数量未发生变化。")
    for change in changed:
        before = "—" if change.before_sellable is None else _rng(change.before_sellable.as_dict())
        after = "—" if change.after_sellable is None else _rng(change.after_sellable.as_dict())
        lines.append(
            f"  {change.channel} / {change.sku}：可确认可售 {before} → {after}"
            f"，风险 {RISK_LABELS.get(change.before_risk or '', change.before_risk or '—')} → "
            f"{RISK_LABELS.get(change.after_risk or '', change.after_risk or '—')}"
        )
    if revision.allocation_changes:
        lines.append("  营销资源变化：")
        for item in revision.allocation_changes:
            action = "保留原决策并转人工" if item["change"] == "protected_review" else (
                "新增" if item["change"] == "new" else "重新分配"
            )
            lines.append(f"    · {item['id']}：{action}")
    if revision.after.manual_items:
        lines.append("  已确认安排进入人工处理：")
        for item in revision.after.manual_items:
            lines.append(
                f"    · {item['id']} {item['sku']} @ {item['channel']}：{item['conflict']}"
            )
    if revision.issues:
        lines.append("  事件处理事项：")
        for issue in revision.issues:
            lines.append(f"    · [{SEVERITY_LABELS.get(issue.severity, issue.severity)}] {issue.message}")
    return "\n".join(lines)
