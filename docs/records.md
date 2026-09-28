# 模型交易与计算记录

Mercury 自动记录每轮模型决定、实际执行的本地计算和模拟成交。模型理由与计算证据分别保存；所有成交仍是本地假钱交易。记录模块没有券商接口，不启动行情采集、模型调用或后台任务。

## 启用与旧数据

新建账户默认带有 `mercury-records-v1` 记录表。继续使用现有 `cycle`、`backtest`、`compare`、`match`、`cancel` 流程即可自动记录，不需要额外运行记录守护进程。

旧账户需要显式增量升级；命令先在 SQLite 写锁保护下备份、检查数据库完整性并比较逻辑内容，再在事务内新增记录表。不会修改历史订单、成交、资金、持仓或事件。备份路径必须不存在，失败时保留备份；修复后使用新的备份路径重试。已经启用的账户再次升级返回 `already_enabled`。

```powershell
python -m mercury --db data/cn-forward.db records upgrade --backup data/cn-forward-before-records-001.db
```

未升级账户保留原有账本读取与执行兼容性；启动新的模型 cycle 会明确要求升级。已有完整历史收据仍可通过原流程校验恢复，不会因此伪造当时的计算明细或代码版本。

如需查询已存在的完整历史 cycle，可显式补录：

```powershell
python -m mercury --db data/cn-forward.db records import-run --path data/old-run/cycle-0000
```

补录验证原 manifest、文件 SHA、阶段引用、Observation/策略/意图、账户路径绑定、账本意图及一致性检查。导入标记 `origin=imported_history`、`code=null`，未保存过的计算标为 `not_recorded`，不会事后重算成“原始证据”。仅接受原绑定账户的完整收据；移动过路径或不完整的旧运行不会猜测归属。已有不同模型归属时拒绝覆盖。

## 查询与导出

```powershell
python -m mercury --db data/cn-forward.db records list --limit 50 --offset 0
python -m mercury --db data/cn-forward.db records list --model MODEL --symbol 600519.SH --status filled
python -m mercury --db data/cn-forward.db records list --since 2026-09-24T00:00:00+08:00 --until 2026-09-28T23:59:59+08:00
python -m mercury --db data/cn-forward.db records show --cycle-id CYCLE_ID
python -m mercury --db data/cn-forward.db records show --fill-id FILL_ID
python -m mercury --db data/cn-forward.db records export --format json --out reports/records-001.json
python -m mercury --db data/cn-forward.db records export --format csv --out reports/records-csv-001
```

`list` 与 `export` 支持 `--model`、`--experiment`、`--cycle-id`、`--symbol`、`--since`、`--until`、`--status`。时间过滤针对轮次决策截止时间；标的过滤针对轮次股票池，保留该轮完整上下文，不截断组合计算。`experiment` 默认是 cycle 输出目录的父目录名称；它是筛选标签，身份以稳定 `cycle_id` 和账户 ID 为准。

`status` 显示当前可查状态，包括 `holding`、`risk_rejected`、`waiting_for_qualified_quote`、`partially_filled`、`filled`、`closed_with_remainder`、`failed`、`incomplete`、`reused_intent`。详情同时保留原 `receipt_status`、`workflow_status` 和当前 `execution_status`，避免后续成交覆盖原来的历史收据。失败后完成恢复，失败事件仍保留。

查询使用只读 SQLite 一致快照，不调用会过期订单的 `portfolio()` / `reconcile()`。一次命令只访问指定账户；不混合 CNY/USD、A股/FDM或不同账户。历史无法归属的成交在无过滤导出中保留，`list` 报告其数量，`show --fill-id` 明确返回未知归属。按模型筛选时不把无法绑定的全账户快照混入结果。

JSON 保留完整结构。CSV 输出 `cycles.csv`、`calculations.csv`、`orders.csv`、`fills.csv`、`snapshots.csv`，嵌套证据以 JSON 单元格保存；`manifest.json` 记录协议、账户、币种、截止时间、账本链头、来源快照哈希和各文件 SHA-256。公式起始字符及首部制表符/换行加文本前缀；CSV中的负数也按文本导出，以避免电子表格求值。需要无前缀十进制值时使用 JSON。

导出采用完整文件原子发布，同路径相同内容可恢复，不同内容拒绝覆盖。CSV 目录以最后发布的 manifest 为完成标志；中断后可用同一来源快照继续，来源已经变化则使用新目录。已有历史产物不修改。

## 记录内容与身份

- 每轮：冻结 Observation、绑定账户、模型输入哈希、已校验研究与策略、意图、风控、原始轮次收据和固定脱敏失败阶段。
- 计算：信号窗口、实际价格与 quote ID、权重和综合值、排名与选择原因、权益和目标权重、数量取整、可卖量、限价依据。`calculations.json` 与 SHA 保存在轮次目录，也进入统一查询。
- 订单：提交时行情、市场规则资料、现金与挂单预留、T+1可卖量、费用估算、风险实际值及阈值。整批拒绝后保留已经执行的预检证据，标记事务回滚。
- 执行：已执行的报价、延迟、容量、价格舍入、费用计算、未成交/拒绝原因。没有运行到的步骤标为 `not_executed`，不填造值。
- 每笔成交：原订单/意图/cycle归属、行情时间与采集时间、来源 SHA、滑点与价差、费用分项及累计计费、部分成交与剩余量、现金/持仓/成本前后值、已实现盈亏和当时估值。

精确金额和计算数值使用 Decimal 字符串；数量为整数。已有统计报告的浮点描述指标仍按原格式保留，不假称精确十进制重算。现有报告对缺失估值和 Sharpe 的限制保持不变。

模型身份默认继承现有适配器声明。若决定由当前 Codex 会话通过离线 JSON 模板传入，可在 agent 配置添加以下字段，其余 provider/strategy/research 字段保持原格式：

```json
{
  "provenance": {
    "model": "实际模型标识",
    "decision_maker": "codex-session",
    "session_source": "实际会话标识"
  }
}
```

这些是宿主声明，不是模型身份认证。不得用虚构标签填补未知身份；未声明的实际决策者/会话来源为 null。`adapter=offline` 始终保留，不冒充外部模型 API 调用。

cycle 的代码哈希仅代表记录开始时；编译计算、风险决定和每笔成交另存实际运行阶段的代码哈希，恢复后不会把新代码执行归为旧代码。代码、配置与来源哈希用于一致性核对，不构成上游数据真实性或独立签名证明。

## 事务与兼容性

记录不另建经济账本。订单、成交及其计算记录在同一交易事务提交；记录写入异常导致事务回滚。模型/cycle阶段记录先持久化，执行后报告失败通过已有意图、原始计算产物和已落库成交恢复。`record_intent_links` 在首次提交意图事务中绑定所有者，另一模型重复提交相同意图不会夺取成交归属。

模型看见的公开账户输入和交易接口不添加任意文件、代码或 SQL 能力。未经校验的模型原文、网络异常体、密钥和隐藏思维过程不进入新记录。来源资料仍为不可信数据。

验证命令：

```powershell
python -m unittest discover -s tests -v
python -m mercury --help
python -m mercury records --help
```

测试使用临时数据库，覆盖合成 A股买卖/T+1、费用和成本复算、跨轮归属、部分成交、并发、撤单、拒绝/持有/失败、写入失败与恢复、旧库备份/导入、查询不写入、CSV注入和不可覆盖导出。合成测试不代表真实 MCP 或真实前向交易验证。
