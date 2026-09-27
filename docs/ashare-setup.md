# A 股实验入口

A 股是主要实验市场。ashare-mcp 仅采集与研究；本地 CNY 账户管理假钱。Financial Datasets 保留为全球行情基础/独立 USD 对照。两类账户不混仓、不自动换汇。

## 安装和调用

已在本机隔离安装 CharmYue/ashare-mcp 固定 commit `614b380606e18eaf35747a0c3b2f27ec551caaa3`。依赖、来源和实际网络结果见[安装审查](ashare-install-review.md)。无公网 HTTP 服务、无隧道；未改变 Auto Review。

```powershell
python -m mercury ashare-tools
python -m mercury ashare-collect 600519.SH --tool get_daily_kline --start 2026-09-24 --end 2026-09-25 --out data/captures/cn-600519-20260924.json
python -m mercury ashare-analyze 600519.SH data/captures/cn-600519-20260924.json --asof 2026-09-24T15:00:00+08:00
```

`ashare-collect` 保留失败响应，输出 `usable_for_execution:false` 或验证错误时不能当行情成功。默认强制 `adjust=""`（不复权）。`source` 是上游自述来源，不是底层供应商签名。捕获的是完整 MCP 解析响应，不冒充 HTTP 原始字节。

本机运行位置由 Git 忽略的 `data/integrations/ashare-runtime.json` 指定，含隔离 Python、启动器、固定 commit、源码清单及启动器 SHA-256。启动前核对实际文件；捕获同时保留 runtime identity。主仓核心仍只用标准库；MCP SDK 在单独 venv 中。其他机器需要按安装审查准备自己的隔离运行环境和绝对路径，不能复用本机路径或依赖联网动态最新版。

Codex 用户配置已新增 `mercury-ashare` 本地 stdio 注册。配置修改前做了用户目录备份，其他设置保持不变。新会话/重新加载后应检查工具可见性；当前成功的实测是隔离 stdio 和 Mercury CLI 桥，不据此声称本聊天已热加载原生工具。

## 可直接运行的合成账户示例

以下价格和证券状态均为**合成测试数据**，使用真实格式的代码仅为演示。不能用于真实行情结论，live 账户拒绝 synthetic 证券资料。

```powershell
python -m mercury --db data/cn-demo.db init --market CN --mode demo --calendar config/cn-calendar-2026.json --start 2026-09-24T10:00:00+08:00 --cash 100000 --fee-bps 3 --minimum-commission 5 --slippage-bps 2
python -m mercury --db data/cn-demo.db register-instrument examples/cn-instrument.json
python -m mercury --db data/cn-demo.db register-session examples/cn-session.json
python -m mercury --db data/cn-demo.db import-fixture examples/cn-quote-1000.json
python -m mercury --db data/cn-demo.db buy 600519.SH 100 --limit 11 --request-id cn-demo-buy-001
python -m mercury --db data/cn-demo.db import-fixture examples/cn-quote-1001.json
python -m mercury --db data/cn-demo.db advance 2026-09-24T10:01:00+08:00
python -m mercury --db data/cn-demo.db match
python -m mercury --db data/cn-demo.db hold --reason "T+1 holding"
python -m mercury --db data/cn-demo.db report
python -m mercury --db data/cn-demo.db audit
```

重复演示请使用新的数据库路径，不覆盖旧实验。`init` 未指定 `--market` 时沿用旧 US/USD 默认，以兼容已有命令；A 股实验须显式指定 `--market CN`。

## 证券分类和逐日状态

下单前必须注册有来源的证券分类与逐日状态。股票与不同 ETF 类别使用 `config/cn-rules.json` 的独立 profile。source 必须有 `symbol`、`reference`、带时区的 `available_at`；每日 source 还须有匹配的 `day`。资料可以预存，但在下单/成交时不能使用当时尚未公开的资料。手填来源和哈希不是外部认证，严格实验应让独立采集子代理核验资料。

证券字段：`symbol`（必须带 `.SH/.SZ/.BJ`）、`profile`、`source`；ETF 另需有证据的 `etf_limit_percent`，不能仅从名称推断 10%/20% 或 T+0。每日字段：`symbol/day/reference_price/suspended/risk_warning/listing_session/source`。`reference_price` 应为当日交易所涨跌停计算参考价，遇除权不能盲目沿用昨日收盘。

- 规则有效区间为 **2026-07-06 至 2026-12-31**；日历覆盖 2026。区间外拒绝，不自动套新规则回放旧历史。旧时期/未来年份需要经过核实的新规则版本。
- T+1 按交易日历记录每批买入的 `available_day`，扣除卖出和未撤卖单冻结；`portfolio` 同时给出 `sellable_qty` 与 `available_to_sell`。9/24 买入股票在中秋假期后 **9/28** 才解锁，9/30 买入在 **10/8** 解锁。
- 沪深主板当前普通及风险警示股票 10%，创业板/科创板 20%，北交所 30%；沪深 IPO 前5交易日、北交所上市首日无日涨跌幅限制。上市阶段须有来源，不从价格猜。沪深风险警示股票还有成交加未撤申报累计50万股的当日买入检查。
- 主板/创业板/ETF 买入100倍数；科创板至少200、北交所至少100，按1股递增。卖出按对应最小量与递增规则检查；不足最小量必须一次卖出全部可卖余额。单笔最大量、tick、ETF回转类型都来自 profile。
- 只支持连续竞价限价单：09:30–11:30、13:00–14:57，北京时间。无保护市价单和集合竞价撮合均拒绝；09:20–09:25、14:57–15:00禁止撤单。
- CN 订单为 DAY。`advance`、下单、撮合、账户查看及报告会协调已确定过期的订单并释放预留；这会新增 expiry 事件，不改历史成交。内建 `audit` 始终只读。
- 无盘口时，涨停方向买入、跌停方向卖出保守不成交；不是宣称涨跌停时所有方向都不能成交。
- 佣金按账户配置，默认最低5元；股票另计双边过户费0.001%、卖出印花税0.05%，ETF份额二级买卖不套股票税费。佣金按含经手/证管的配置口径，避免重复加收；新账本按原订单累计金额分别四舍五入到分，部分成交仅收累计费用增量。这是明确模拟口径，未核实所有券商结算方式。卖出后资金不足以支付费用/保留其他订单资金时拒绝成交，不制造负现金。

官方规则、生效日期与费用原文见[来源审查](ashare-rules-sources.md)；不是对完整交易所撮合的复刻。公司行为、盘中临停、价格笼子、盘口排队、适当性和 ETF 申赎尚未实现。部分成交、价差和延迟现可显式配置，但未按真实盘口校准，见[流程指南](agent-workflow.md)。

## 当前证据与限制

真实 stdio 当前发现31工具（固定上游30个及Mercury时间戳扩展1个）。此前取得600519在2026-09-24的1条未复权日线；新增扩展已成功取得报价数值1237及同一响应的供应商时间。9/25–27中秋休市，缺少这些自然日的记录正常。原有quote/minute/info的连接失败证据保留，不能用假期解释，也不能因扩展成功就宣称原工具均已恢复；详见[网络修复记录](ashare-network-repair.md)。

隔离进程原样继承宿主的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`，包括受限环境中的路由限制；不自行创建直连例外或改变 Windows 系统代理。带用户名/密码的代理 URL 暂不支持，启动前固定脱敏拒绝，避免上游异常把凭证写入原始响应。FDM/Tushare 等无关密钥仍不传给 A 股子进程；网络路由值不写入 capture。

固定上游实时快照未提供完整可靠行情时间。`ashare-quote` 现调用新增的 `mercury_get_realtime_quote` 本地扩展，从东方财富同一响应的 `f86` 取得供应商交易时间，按 `f59` 精度解释 `f43`，核对 `f57/f107` 身份；官方字段依据见[来源审查](ashare-timestamp-source.md)。原有工具仍保留，固定源码没有修改。

该扩展目前只支持明确 `.SH/.SZ`；`.BJ` 尚未验证，明确拒绝。每次最多3个证券、每个请求10秒超时和1MiB上限、不重试、不跟随重定向。成功和业务失败均保留有界原文及哈希，原文不会作为指令执行。供应商缓存状态为未知，不标为最新或无缓存。本地再次从原始响应重算价格、时间、身份、币种和来源字段，拒绝不一致数据。

扩展源码在本地独立目录保存并校验哈希；新机器必须安装相同已审查扩展，不能只配置原版30工具。`ashare-collect ... --tool mercury_get_realtime_quote --out <新路径>` 可以保存原始响应；若供应商未提供明确CNY标记，无账户契约的单独采集会报告币种未验证。交易采集使用预先注册且可用的 CNY 证券契约，不猜币种或状态。

缺失、未来、陈旧行情及休市时段仍拒绝成交，不以抓取时间补齐。代码验证和工具发现不代表真实即时行情成交已成功。

已用公司披露与上交所资料核实600519.SH的当前主板A股/CNY契约，资料可用时间取本次核实时间，不回填历史。保存的真实快照在独立新账户中正常入库，供应商时间为9月24日16:11:44；陈旧行情及9月27日休市买单正确拒绝，账户保持100000假钱、0成交。没有据此声称完成正向买卖或前向收益测试；交易日仍须先取得有效的逐日状态资料和新鲜行情。

日线归一化为所属交易日15:00、标记 `session-close`；分钟线保留原时刻，并因上游未保证bar-end而保守延迟一个周期作为可用时间。**日线目前仅用于研究/估值，不能用作该连续竞价模型的撮合报价**：15:00不在支持时段，次日又不能复用上日行情。分钟线也需满足可用时间、同交易日、合法时段、资料状态及新鲜度。

`ashare-load` 只允许 demo/replay CNY 账户并原子导入；需要先注册证券。它不证明数据完整，也不实现对拥有文件访问权的 agent 的盲测隔离。模型收益能力、真实前向效果和完整市场数据集均未验证。

`ashare-analyze` 的 `max_drawdown` 与 FDM 分析及账户报告统一表示非负回撤幅度，例如从 10 降到 8 返回 `0.2`；`price_return` 仍保留涨跌符号。历史价格统计不含交易费用，不能替代模拟账户净收益。
