# Mercury

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

供 agent 使用的本地量化模拟账户：**ashare-mcp 支持 A 股研究，Financial Datasets MCP 保留为全球行情基础，Python 管理独立的 CNY/USD 假钱账户**。无券商连接，无真实下单。核心仅依赖 Python 3.11+ 标准库。

> 项目状态：已实现 A 股研究、策略、风控、执行与复盘闭环，见[流程指南](docs/agent-workflow.md)与[验收证据](docs/repository-acceptance.md)。真实带时间戳的 MCP 采集已验证；真实前向成交和模型 API 调用仍未验证。FDM 认证已通过，最近一次检查的行情访问受 credits/balance 限制。

```text
Collector subagent -> ashare-mcp (CN) / FDM (global) -> Raw response / time / SHA-256
                                                        |
                                             Local validation and analysis
                                                        |
Frozen Observation -> Research / Strategy JSON -> TradeIntent -> Hard risk limits
                                                        |
                                  SQLite paper fills -> Review -> Next observation
                                                        |
                                           Independent audit + primary review
```

## 完整 A 股 agent 流程

```powershell
python -m mercury workflow-demo
```

合成示例让两个离线 agent 使用相同初始 CNY 资金、观察、费用与规则，各完成跨越9月25–27日休市的T+1买卖。不调用模型 API 或真实行情。[AGENTS.md](AGENTS.md) 已明确每轮必须采集、冻结、研究、编译意图、执行风控、撮合、复盘及独立审计。

`cycle` 支持有限轮次，可使用离线 agent、OpenAI Responses API 或显式 HTTPS JSON 服务，凭证仅来自环境变量。模型接收冻结数据并返回严格策略 DSL，没有本地执行工具。`backtest` 和 `compare` 使用独立账户、共享观察进行时序回放。配置与 JSON 格式见[流程指南](docs/agent-workflow.md)。

CNY 账户可用 `--risk-policy` 固定仓位、单笔金额、日换手及日亏损上限；启用后拒绝直接 `buy`/`sell`，必须通过已验证的 TradeIntent 整批提交。`--execution-policy` 可配置价差、延迟和部分成交，共享同一行情容量、按订单累计收费。这些是模拟参数，没有声称还原真实盘口。合成演示不证明实际模型 API 可用或前向盈利。

## A 股实验（主要市场）

A 股采用 **ashare-mcp → 人民币独立账户**。Financial Datasets 保留为全球行情基础及独立美元对照，主实验不跨市场、跨币种混仓。

```powershell
python -m mercury ashare-tools
python -m mercury --db data/cn-demo.db init --market CN --mode demo --calendar config/cn-calendar-2026.json --start 2026-09-24T10:00:00+08:00
python -m mercury --db data/cn-demo.db register-instrument examples/cn-instrument.json
python -m mercury --db data/cn-demo.db register-session examples/cn-session.json
python -m mercury --db data/cn-demo.db import-fixture examples/cn-quote-1000.json
python -m mercury --db data/cn-demo.db buy 600519.SH 100 --limit 11 --request-id cn-demo-buy-001
python -m mercury --db data/cn-demo.db import-fixture examples/cn-quote-1001.json
python -m mercury --db data/cn-demo.db advance 2026-09-24T10:01:00+08:00
python -m mercury --db data/cn-demo.db match
python -m mercury --db data/cn-demo.db report
python -m mercury --db data/cn-demo.db audit
```

示例使用**合成报价和证券状态**，不是真实标的价格。CN 下单须有来源明确的证券分类、每日停牌/风险/上市阶段/涨跌停资料，并绑定标的、日期和可用时间。日历覆盖 2026，当前规则仅适用于 **2026-07-06 至 2026-12-31**。9 月 25–27 日中秋休市，9 月 24 日买入的股票在 9 月 28 日才可卖。

已支持 T+1 持仓批次、有证据的 ETF 分类 T+0、可卖及冻结数量、按板块的数量/tick/单笔限制、时效化涨跌停和佣金/过户费/印花税分项。CN 仅支持连续竞价限价单，不支持集合竞价与无保护市价单；无盘口时保守拒绝涨停买入、跌停卖出。DAY 订单过期释放预留，账户查询/报告可能追加过期事件；`audit` 始终只读。

已注册 Codex 本地 stdio 服务 `mercury-ashare`，可能需要新聊天/重新加载才显示。核心仍只用标准库，独立审查的 MCP 环境与本机路径见 [A 股接入说明](docs/ashare-setup.md)。启动前检查源码清单与启动器哈希。

真实证据：发现原有30工具及1个Mercury扩展，取得9月24日日线和带供应商时间的真实快照。快照时间是9月24日16:11:44（北京时间），在25–27日休市期间已陈旧；币种缺失须由已注册CNY证券契约绑定。`ashare-quote`现使用`mercury_get_realtime_quote`，从同一原始响应复核价格、时间、身份，并记录独立扩展哈希。原有quote/minute/info失败证据保留。日线仍仅用于研究/估值，真实前向成交未验证。详见[字段来源](docs/ashare-timestamp-source.md)和[独立审计](docs/ashare-audit-review.md)。

## 立即运行

在仓库根目录执行，不需要 pip install：

```powershell
python -m mercury demo
python -m unittest discover -s tests -v
python -m mercury --help
```

`demo` 使用临时合成行情，演示买入、撤单、卖出、继续持仓、统计和审计，结束后清理它自己的临时账户。它不是行情接入测试。

## 本地账户

```powershell
python -m mercury --db data/demo.db init --mode demo --start 2026-01-05T15:00:00Z
python -m mercury --db data/demo.db import-fixture examples/quote-100.json
python -m mercury --db data/demo.db buy DEMO 10 --request-id decision-001 --reason "Demo buy"
python -m mercury --db data/demo.db import-fixture examples/quote-101.json
python -m mercury --db data/demo.db advance 2026-01-05T15:01:00Z
python -m mercury --db data/demo.db match
python -m mercury --db data/demo.db hold --reason "Maintain position"
python -m mercury --db data/demo.db portfolio
python -m mercury --db data/demo.db report
python -m mercury --db data/demo.db audit
```

- `buy` / `sell` 只提交订单；`match` 显式撮合。成交价由已记录报价、滑点和限价共同决定，交易命令没有成交价参数。
- `--limit 99` 是最高买价或最低卖价，不是强制成交价。滑点后不满足限价则保持挂单。
- `cancel <order_id>` 撤销未成交订单，释放预留。已成交订单不可撤销。
- `hold` 记录继续持仓的决定与估值；不撤销已有挂单。重启后现金、订单、持仓都还在。
- `--request-id` 必填。同一决定重试复用 ID；改变参数却复用 ID 会报错。
- 一库一账户/实验。初始 100,000 USD；手续费默认 5 bps、滑点默认 2 bps（1 bp = 0.01%），可在 `init` 设置。后续不修改已建账户配置。
- 整数股、多头、无杠杆，新账户可配置部分成交；买单预留现金，卖单预留剩余股数，SQLite 写事务串行化。市价跳空超出可用现金时拒绝订单。
- demo/replay 必须等待下单之后的一条行情；live 使用截至当前时间最近且未过期的 MCP 报价。最长报价年龄默认 300 秒，超时不成交；持仓无新鲜估值时净值返回 `null`。
- 金额用 Decimal，记账精度 8 位小数；买入手续费计入持仓成本，卖出按平均成本分摊，报告净收益包含双方费用。
- 默认不会运行后台循环或定时任务；agent 通过 CLI/API 发起每个采集、撮合和报告步骤。

## 真实行情 → 假钱模拟

在本机设置环境变量 `FINANCIAL_DATASETS_API_KEY`。Windows 路径为**编辑账户的环境变量 → 用户变量 → 新建**，用该名称填写密钥值，不要修改 `PATH`。重启终端/Codex 后继承变量，详情见[认证说明](docs/market-setup.md)。

代码不读取密钥文件，不接受命令行密钥，不写 `.env`。不要把密钥放入源码、行情记录、聊天或 Git。

```powershell
python -m mercury mcp-tools
python -m mercury --db data/forward-agent-a.db init --mode live
python -m mercury --db data/forward-agent-a.db collect-quote AAPL --currency USD
python -m mercury --db data/forward-agent-a.db buy AAPL 1 --request-id forward-001 --reason "Your strategy rationale"
python -m mercury --db data/forward-agent-a.db match
python -m mercury --db data/forward-agent-a.db snapshot
python -m mercury --db data/forward-agent-a.db audit
```

`live` 只表示真实时间/MCP 行情模式，**仍是全程假钱**。采集与交易分开，MCP 没有下单权限。`--currency USD` 是你对该标的报价币种的明确约定：官方快照 schema 可能不返回 currency，因此不能静默猜币种。返回其他币种会拒绝。

当前采用[官方托管 MCP](https://docs.financialdatasets.ai/mcp-server) `/api`，先初始化、发现工具，再调用 `get_stock_price` / `get_stock_prices`。JSON 与 SSE 响应均支持；未识别的 schema、缺失时间或来源错误均拒绝成交。真实服务的权限、配额和输出格式必须连通验证，mock 测试不替代这一步。

## 历史采集与本地分析

```powershell
python -m mercury collect-history AAPL --start 2025-01-01 --end 2025-01-31 --out data/captures/aapl-202501.json
python -m mercury analyze-history AAPL data/captures/aapl-202501.json --asof 2025-02-01T00:00:00Z
python -m mercury --db data/replay-a.db init --mode replay --start 2025-01-01T00:00:00Z --max-age 86400
python -m mercury --db data/replay-a.db load-history AAPL data/captures/aapl-202501.json
```

历史导入是原子的，任何条目失败整批回滚。导入后按实际数据日期 `advance`，先观察和决策，再前进到之后的行情并运行 `match`。日线在下一天 UTC 零点起可用，不能在当天开盘时看到当天收盘价。本地分析计算价格收益、回撤、20 期均线、样本收益波动，自动按 `asof` 截断。

`collect-history` 保存一次工具响应，**不是全市场下载器**。只在响应明确给出 `next_page_url: null` 时标记这次响应分页结束；缺分页信息视为未知。有下一页时需要继续收集或缩短时间窗口，不能据此宣称完成 5–10 年数据集。详情见 [评测约定](docs/evaluation.md)。

## 审计与实际边界

`audit` 只读校验事件哈希、来源绑定、订单状态、成交费用/滑点/限价，独立重建现金、持仓、成本和已记录估值。非零退出码表示检查未通过或发生错误。它是本地一致性检查；独立子代理审计另按 [AGENTS.md](AGENTS.md) 执行，不能替代 Codex Auto Review。

SHA-256 证明本地内容一致性，不是供应商签名。相同进程 Python 私有方法与本机数据库不构成对恶意 agent 的权限隔离；严格评测需独立运行器、只读观察接口和不可写的外部证据锚点。

尚未实现：美股交易日历/闭市限制及集合竞价机制、真实盘口深度/队列模拟、市场冲击、拆股分红、退市与幸存者修正、匿名基准运行器、大规模历史数据集和长期前向评测。因此当前是可测试的模拟基础设施，不是可据以声称投资或模型表现的完成版 benchmark。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `mercury/` | 交易引擎、行情适配、分析、审计和 JSON CLI |
| `tests/` | 本地回归测试 |
| `examples/` | 明确标记的合成行情 |
| `docs/` | 接入、评测约定和独立审计报告（简体中文） |
| `scripts/` | 使用环境变量的 Windows 连接检查 |
| `data/` | 私有实验数据库与行情响应，Git 忽略 |
| `.github/workflows/checks.yml` | Python 3.11 / 3.13 的本地测试与演示 |

创建账户或行情记录时拒绝覆盖已有文件，不自动提交或推送。

## 开发

提出修改前，执行快速开始中的验证命令并阅读 [AGENTS.md](AGENTS.md)。使用功能分支和 PR，保留已有数据与修改。交易执行、计费、数据接入或评测发生实质变化时，需要独立子代理审计。三份 README 须同步更新。本地测试通过不代表远程 CI 或真实 MCP 连接已通过。

## 许可证

[Apache License 2.0](LICENSE)。
