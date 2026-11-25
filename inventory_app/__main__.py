"""命令行入口。

用法::

    python3 -m inventory_app review --data samples --as-of 2026-10-01
    python3 -m inventory_app review --data samples --format json
    python3 -m inventory_app review --data samples --events events.csv
    python3 -m inventory_app review --data samples --budget 20000 --save review.json
    python3 -m inventory_app sample-events --out events.csv

``review`` 输出一次完整评审，或加上 ``--events`` 输出事件修订前后对比；
``--save`` 把结果写入文件而不是标准输出。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import date
from pathlib import Path

from .engine import InventoryEngine
from .events import apply_events
from .inputs import InputError, load_directory, read_events
from .model import Policy
from .render import render_json, render_revision_text, render_text


def _policy_from_args(args: argparse.Namespace) -> Policy:
    overrides = {"as_of": args.as_of}
    if args.budget is not None:
        overrides["budget_total"] = args.budget
    if args.near_expiry_days is not None:
        overrides["near_expiry_days"] = args.near_expiry_days
    if args.min_cover_days is not None:
        overrides["min_cover_days"] = args.min_cover_days
    if args.overstock_cover_days is not None:
        overrides["overstock_cover_days"] = args.overstock_cover_days
    return dataclasses.replace(Policy(), **overrides)


def _run_review(args: argparse.Namespace) -> int:
    try:
        snapshot = load_directory(args.data)
        engine = InventoryEngine(_policy_from_args(args))
        if args.events:
            events = read_events(args.events).events
            revision = apply_events(snapshot, events, engine)
            if args.format == "json":
                output = json.dumps(
                    {"revision": revision.to_dict(), "review": revision.after.to_dict()},
                    ensure_ascii=False, indent=2,
                )
            else:
                output = render_text(revision.after) + "\n" + render_revision_text(revision)
        else:
            review = engine.review(snapshot)
            output = render_json(review) if args.format == "json" else render_text(review)
    except InputError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    if args.save:
        Path(args.save).write_text(output, encoding="utf-8")
        print(f"已写入 {args.save}")
    else:
        print(output)
    return 0


def _run_sample_events(args: argparse.Namespace) -> int:
    """生成一份事件样例，演示盘点、退货与调拨签收。"""
    rows = [
        "id,sku,kind,qty,location,batch,day,state,target,note",
        "E1,S1,count,18,ST1,B2,2026-10-05,saleable,,门店盘点",
        "E2,S2,return,3,ST2,,2026-10-05,saleable,,顾客退货",
        "E3,S1,receipt,,ST1,,2026-10-05,,T1,调拨签收",
    ]
    content = "\n".join(rows) + "\n"
    if args.out:
        Path(args.out).write_text(content, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(content, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inventory_app",
        description="库存健康信号与营销资源释放联动",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="执行一次库存健康与营销资源评审")
    review.add_argument("--data", required=True, help="包含 inventory.csv / commitments.csv / sales.csv 的目录")
    review.add_argument("--events", help="可选的事件 CSV（盘点、退货、调拨签收）")
    review.add_argument("--as-of", type=date.fromisoformat, default=None, help="评审日，默认取最新库存快照日期")
    review.add_argument("--format", choices=["text", "json"], default="text")
    review.add_argument("--save", help="把结果写入文件而不是标准输出")
    review.add_argument("--budget", type=float, default=None, help="促销预算上限")
    review.add_argument("--near-expiry-days", type=int, default=None)
    review.add_argument("--min-cover-days", type=float, default=None)
    review.add_argument("--overstock-cover-days", type=float, default=None)
    review.set_defaults(func=_run_review)

    events = sub.add_parser("sample-events", help="生成事件 CSV 样例")
    events.add_argument("--out", help="输出文件；省略时写到标准输出")
    events.set_defaults(func=_run_sample_events)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
