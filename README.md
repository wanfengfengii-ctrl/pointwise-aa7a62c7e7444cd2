# 水质观测湖 V1

市政水务监测中心的设备用字节码发送读数。站端值班员登记码表并验收原页，归档员按当日申报顺序冻结和签发，外站仓管员离线接纳并整理物理页，夜班按已签成果号核查明细和合计。站库与湖库均使用本地 SQLite，可单独重开。首次打开安装表，后续打开保留用户已提交的数据。样例通过演示显式加载。

## 运行

使用 Python 3.11 和标准库，从项目根目录运行完整回归及演示。

```sh
python3 -B check_project.py
python3 -B demo.py
```

完整回归入口由运行平台提供。演示使用临时站库和湖库，从 `samples/station-mixed.json` 签发并关闭站库，再将交付件送入湖库。另一份混代件从 `samples/independent-mixed.json` 独立接纳。两份成果都以页大小 3 合卷，重开后按号打印实际明细和合计。D1 和 D2 的两张原页都是 `[0,1]`，应分别读出 `[5,9]` 和 `[11,13]`，合计 38。合卷后的第三行仍来自 `A-later` 的第 0 槽和 D2。另两份样例用于零值缺测核账和缺证据拒收。

## Python 入口

| 工作 | 公开入口 | 成功结果 |
| --- | --- | --- |
| 打开站库 | `source.store.open_source(path)` | 已初始化的站库连接 |
| 定版 | `catalog.dictionaries.register(conn, version, mapping, unit, missing_code=None)` | 固定 `Codebook`，可用同模块 `get_dictionary(conn, version)` 查用 |
| 验页并冻结 | `source.freeze.freeze(conn, batch_id, unit, pages)` | 冻结批号，可用同模块 `get_frozen_batch(conn, batch_id)` 查询 |
| 签发 | `exchange.issue.issue(conn, batch_id)` | `IssuedPackage`，含 `issue_number`、`batch_id` 和 `body` |
| 导出 | `exchange.exports.export_by_issue_number(conn, issue_number)` | 原 V1 正文字节 |
| 打开湖库 | `lake.store.open_lake(path)` | 已初始化的独立湖库连接 |
| 接纳 | `lake.admission.accept(conn, body)` | `LakeRelease`，含 `result_number`、`batch_id`、`unit` 和 `receipt` |
| 合卷 | `lake.repack.repack(conn, result_number, page_size)` | 已提交的物理页数 |
| 领取 | `audit.reconcile.read(conn, result_number)` | `AuditReport`，含成果头、明细 `details` 和核账 `totals` |

通过 `source.pages.SourcePage(page_id, dictionary_version, codes)` 构造页，其中 `codes` 为 `bytes`。若需先验收再组批，可在 `source.store.source_transaction(conn)` 内先调用 `source.page_validation.validate_page(conn, page, unit)`，再调用 `source.pages.store_page(conn, page)`。冻结会再次校验整批。一个站库内，同一原页号允许引用同一页，异内容重报拒绝。批号唯一，批内原页号唯一。字典版本、单位和标识使用非空白 UTF-8 字符串，按原字符串比较。批次单位与引用的每份字典完全一致，计算沿用该单位。连接用完由调用方关闭。定版、冻结、签发、接纳、合卷和领取各自拥有事务，直接调用这些入口即可。

## V1 交付件

正文为 UTF-8 JSON 对象。顶层含 `version`、`batch_id`、`unit`、`pages`、`dictionaries`。`version` 为整数 1。`pages` 按冻结顺序列出对象，每个对象含 `page_id`、`dictionary_version` 和 `codes`。`codes` 是有序字节整数数组，当前位置即原槽位，每个页号在件内唯一。`dictionaries` 是声明数组，每条含 `version`、`unit`、`entries`、`missing_code`。`entries` 是 `[码,读值]` 的数组，码的取值为 0 到 255。普通读值使用 JSON 整数，也可用 `0x` 或 `-0x` 开头的规范小写十六进制字符串表示任意精度整数。`missing_code` 为独立字节或 `null`。同版同义声明可重用，异义声明使整件无效。每页必须能从本件找到实际写入版，包括槽数组为空的原页。空批可领取空明细。

```json
{
  "version": 1,
  "batch_id": "shift-example",
  "unit": "mC",
  "pages": [{"page_id": "probe-1", "dictionary_version": "D0", "codes": [0,255]}],
  "dictionaries": [{"version": "D0", "unit": "mC", "entries": [[0,0]], "missing_code": 255}]
}
```

字典映射可含负整数或零。只有缺测码读出 `None`。报告按原批页序和原槽序返回 `ordinal`、`value`，以及 `origin` 中的 `page_id`、`slot` 和 `dictionary_version`。`totals` 的 `non_missing_count` 与 `missing_count` 分别统计实数值和缺测，`total` 使用精确整数求和。全部缺测和空批的合计为零。物理页大小需为正整数，合卷只切分相同的固定行。成果号与回执属于本次湖库接纳，重复提交合法件会生成另一固定成果。

输入定义冲突、错误单位、无效编码、缺解释依据或非法物理页大小抛出 `ValueError`。未知冻结批、发行号、成果号及显式码表查找抛出 `KeyError`。SQLite 失败向调用方报告原数据库异常。接纳失败不留下新成果和回执，合卷失败后仍可领取原布局。已签发行件按号导出，字典随后换版也不改变旧件。
