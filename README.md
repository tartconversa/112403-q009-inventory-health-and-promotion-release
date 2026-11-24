# 库存健康信号与营销资源释放联动

合成仓店库存、批次效期、订单预占、调拨及营销计划样例。

All example identifiers and values are synthetic. CSV files use UTF-8. The first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `inventory.csv` contains 仓店商品批次、效期和现存数量。 Columns are `location`, `sku`, `batch`, `qty`, `expiry`, `day`.
- `commitments.csv` contains 订单预占、在途调拨和已确认营销计划。 Columns are `id`, `sku`, `from`, `to`, `qty`, `kind`, `state`.
- `sales.csv` contains 退货调整后的近期净需求和促销投入。 Columns are `sku`, `location`, `units`, `returns`, `spend`, `period`.

Python 3 standard library is sufficient to inspect these inputs. Run `python3 -m unittest discover -s tests -v` to check the delivered file structures and example references.
