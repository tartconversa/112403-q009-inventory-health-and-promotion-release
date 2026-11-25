# 库存健康信号与营销资源释放联动

一个仅依赖 Python 标准库的数据应用：把仓店库存快照、批次效期、在途调拨、订单
预占、退货调整后的净销售与既有营销资源计划放在同一个口径下评审，输出**可确认
数量范围**、库存风险、渠道张力，以及受保护的营销资源分配建议。

所有样例中的标识与数值均为合成数据。

## 问题与口径

按库存总量判断畅销与滞销会同时犯三类错误，本应用分别处理：

| 问题 | 本应用的处理 |
| --- | --- |
| 订单预占被当成可促销库存 | 预占**只扣减**，永不进入可售量；状态未知时按 `[0, qty]` 扣减，给出可用量区间 |
| 调拨在发出方与接收方重复出现 | 在途不并入即时可用；两侧各按 `[0, qty]` 形成“可能重复计入”的扣减，合计不会多出库存 |
| 临期批次被总量掩盖 | 按批次保留效期，临期与过期分别计量；“临期但周转快”判为缺货风险而非积压 |
| 仓、店、电商各自上报 | 同一批次只取最新快照；同一天多份不一致的上报合并为 `[最小, 最大]` 而**不相加** |
| 缺报或状态冲突 | 缺数量 → 已确认 0、上限未知；状态冲突 → 取最保守端；每条都附可确认数量范围 |

关键恒等式（低端用于资源计划，高端用于风险判断）：

```
可确认可售 = 在库 − 封锁/过期 − 订单预占 − 在途可能重复计入
```

## 运行

```bash
python3 -m inventory_app review --data samples --as-of 2026-10-01
python3 -m inventory_app review --data examples/channel_conflict --as-of 2026-10-01 \
    --budget 60000 --format json
python3 -m inventory_app sample-events --out /tmp/events.csv
python3 -m inventory_app review --data examples/channel_conflict --as-of 2026-10-01 \
    --events examples/channel_conflict/events.csv
```

`review` 打印资源评审视图；`--format json` 输出给下游系统的结构化结果；
`--events` 追加一份“事件修订前后对比”。退出码 `0` 正常、`2` 输入错误。

样例数据：

* `samples/`：README 原始三份 CSV，规模最小，用于复现基础口径
* `examples/channel_conflict/`：六渠道/SKU 组合，含电商预占超卖、在途调拨、
  临期批次、封锁库存、已确认与未确认营销资源，以及一份事件 CSV

也可以在代码中调用：

```python
from datetime import date
from inventory_app import InventoryEngine, Policy, load_directory

review = InventoryEngine(Policy(as_of=date(2026, 10, 1), budget_total=60000)).review(
    load_directory("examples/channel_conflict")
)
for report in review.reports:
    print(report.location, report.sku, report.sellable.as_dict(), report.risk)
print(review.manual_items)      # 已确认但需人工处理的安排
```

## 输入

UTF-8 CSV，首行是列名，空单元格表示“来源未提供该值”。`inventory.csv` 与
`sales.csv` 必须存在；`commitments.csv` 可省略（视为没有预占、调拨或营销计划）。

### `inventory.csv`

| 列 | 必需 | 说明 |
| --- | --- | --- |
| `location` | 是 | 仓库/门店/电商渠道，例如 `WH1`、`ST1`、`EC1` |
| `sku` `batch` | 是 | 商品与批次 |
| `qty` | 是 | 现存数量；留空表示缺报（不是 0） |
| `expiry` | 是 | 批次效期；留空时无法判断临期 |
| `day` | 是 | 快照日期 |
| `status` | 否 | `saleable`/`blocked`/`pending` 等。**整列缺失**视为来源不提供状态（按策略默认值并标注假设）；**列存在但该行留空**视为待确认，可售量给出 `[0, qty]` |
| `received` | 否 | 入库日期，用于库存龄；缺失时库存龄显示“—” |
| `substitute_for` | 否 | 替代关系（商品团队确认后用） |
| `note` | 否 | 备注 |

### `commitments.csv`

`id, sku, from, to, qty, kind, state` 必需；`day, slot, budget, min_stock, note` 可选。

* `kind=reservation`：订单预占，`from` 是被占用渠道，`state` 决定是否占用
* `kind=transfer`：调拨，`from` 发出方、`to` 接收方，`state` 取 `in-transit`/`received`/`planned`/`cancelled`
* `kind=marketing`：营销资源，`from`（或 `to`）是投放渠道，`state` 取 `confirmed` 或 `proposed`

`state` 支持中英文；无法识别时按最保守范围处理并列出事项。同一 `id` 多次上报
只取最新日期的一版。

### `sales.csv`

`sku, location, units, returns, spend, period`。净需求 = `units − returns`；
`period` 支持 `YYYY-MM` 或 `YYYY-MM-DD..YYYY-MM-DD`。同一渠道同一商品的多期
记录按天数加权合并，不会相加成更高需求。

### `events.csv`（可选）

`kind` 与 `day` 必需，其余按类型取用：

| `kind` | 作用 |
| --- | --- |
| `count` / `盘点差异` | 以实盘数量替换指定批次快照（`location`,`batch`,`qty`；可带 `state` 改为封锁） |
| `return` / `退货` | 退回货品计入库存，并上调该渠道退货数、下调净需求 |
| `receipt` / `调拨签收` | 把 `target` 指向的调拨改为已签收；给出 `qty` 时补一条接收库存，否则只解除重复计入风险 |

## 评审输出

* **渠道库存与需求**：在库、可确认可售、覆盖率、库存龄、健康信号、风险
* **商品级渠道张力**：同一 SKU 在一个渠道缺货、另一个渠道积压，给出可调数量
* **营销资源分配**：申请量、建议量、优先级、转人工原因、预算合计
* **需人工处理的已确认安排**：已确认但按新库存不可行，保留原决策
* **待确认事项**：每条带严重级别、涉及渠道/批次，以及数量范围

风险分级：覆盖率低于 `min_cover_days` 为缺货，高于 `overstock_cover_days` 为积压，
介于两者为平衡；无净需求且仍有可售库存为积压。

## 资源保护与重排

* **已确认**（`confirmed`/`已签`）资源永不自动改动。库存不足时仍按原量保留，
  同时列为“转人工”，并给出缺口范围，供补货或调整档期。
* **未确认**资源按优先级重排：临期清理 > 滞销去化 > 常规增长 > 缺货渠道（暂缓）。
  每个渠道的可分配上限为 `可确认可售低端 − 最低库存/安全覆盖 − 已确认营销占用量`。
* **预算**超限时，从优先级最低的未确认资源开始释放；已确认资源不受影响。
  未提供预算上限时输出 `budget_unverified`，数量建议照常给出但标注未校验。
* 投放档期（`slot`）原样保留；已确认档期不会因为重算被移动。

## 策略参数

`Policy` 集中放置需要业务确认的阈值，可按团队调整：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `near_expiry_days` | 60 | 临期窗口 |
| `min_cover_days` | 14 | 低于此覆盖率判为缺货，也是促销的安全底线 |
| `safety_cover_days` | 21 | 补货目标的覆盖天数 |
| `overstock_cover_days` | 90 | 高于此覆盖率判为积压 |
| `stale_snapshot_days` | 3 | 快照超过此天数提示复核 |
| `default_inventory_status` | `saleable` | 来源不提供状态列时采用的假设（会在事项中标注） |
| `budget_total` | 无 | 促销预算上限；未提供则预算不校验 |
| `min_stock` | `{location: {sku: qty}}` | 最低库存承诺（渠道级） |
| `min_stock_sku` | `{sku: qty}` | 最低库存承诺（商品级，渠道未配置时回退） |
| `channel_labels` | 空 | 自定义渠道显示名 |

## 代码结构

```
inventory_app/
  inputs.py     读取与结构校验（保留行号、识别缺失列）
  normalize.py  状态/类型归一化，重复上报去重
  supply.py     批次合并、预占扣减、调拨重复计入防护
  demand.py     净需求、覆盖率、临期与风险分级
  marketing.py  资源保护、优先级重排、预算与渠道张力
  reports.py    渠道评审视图与商品级汇总
  events.py     盘点/退货/签收修订及前后对比
  engine.py     编排
  render.py     终端与 JSON 输出
  __main__.py   命令行
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：区间算术、预占不被计入可用量、在途两侧不重复、同日多源不相加、
陈旧快照不叠加、缺报上限未知、状态冲突取保守端、已确认资源保护、预算超限
释放顺序、渠道张力、盘点/退货/签收修订，以及命令行端到端输出。
