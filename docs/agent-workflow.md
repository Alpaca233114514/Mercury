# A股研究与模拟交易流程

主路径是 `Observation → Research / Strategy JSON → TradeIntent → 风控 → 本地撮合 → 复盘 → 下一轮`。强制执行顺序见 [AGENTS.md](../AGENTS.md)，实施计划见[完善计划](ashare-workflow-implementation-plan.md)，验收与独立审计分别记录。以下所有资金和成交都是模拟。

## 先运行合成闭环

```powershell
python -m mercury workflow-demo
```

命令在临时目录创建两个独立CNY账户，共享相同冻结市场观察、初始资金、规则与费用。两种离线策略分别完成9月24日买入、9月28日T+1卖出；25至27日中秋休市。示例不调用模型API或真实行情，输出完整交易分组、费用、换手、价格基准及缺失指标原因。策略收益只用于测试。

保存可重复的输入与输出可先生成示例JSON，再运行：

```powershell
python -m mercury workflow-example --out data/example-cn-001
python -m mercury backtest --episode data/example-cn-001/episode.json --agent data/example-cn-001/agent.json --out data/backtest-001
python -m mercury compare --episode data/example-cn-001/episode.json --agents data/example-cn-001/agents.json --out data/comparison-001
```

`agent.json` 的离线结构为 `{"provider":"offline","name":"strategy-a","strategy":{...}}`；`agents.json` 是名称到各agent配置的映射。每次不同实验使用新目录；相同输入可以恢复，配置不同或已有账本不匹配则拒绝。不要拿示例证券资料登记真实账户。

## 数据、模型与风控

采集子代理通过只读MCP取得原始响应，可信宿主归档并入库。冻结观察仅保留截止时刻已经可用的价格、证券状态与带发布时间/可用时间的研究资料；完整历史capture、内部路径与密钥不进入模型输入。账户状态单独提供，含可卖数量、持有交易日数与公开风控约束。

模型输出严格的研究与策略JSON，本地绑定观察ID并计算内容哈希。DSL支持有限的momentum/sma/volatility信号、等权选择、再平衡、持有期与失效条件。解释器执行固定本地算术，不执行模型生成的代码、SQL、shell或工具调用。历史策略只能在下一条合格行情成交，日线收盘价不能回填为已知价格成交。

账户显式配置 `cn-risk-v1`：`max_position_weight`、`max_order_notional`、`max_daily_turnover`、`max_daily_loss`，以及 `currency: CNY`。单笔金额和风控换手预算包含费用与未成交承诺；统计报告的turnover则是已成交买卖金额/初始资金，不含费用、未年化。日亏损限额阻止新增买入；它不是跳空损失保证，也不强制卖出。缺少持仓估值或历史日基准时禁止新买，不能重新设基准抹掉亏损。

通过 `init --risk-policy <JSON>` 或尚无订单/成交账户的 `configure-risk <JSON>` 一次配置。已启用风控账户必须提交结构化意图，旧 `buy`/`sell` 不能绕过。整批意图在事务内接受或拒绝，不静默裁剪数量；下单与实际成交均复核。CNY账户仍受来源、交易时段、T+1、数量、涨跌停和费用规则约束。

可选 `--execution-policy <JSON>` 设置 `paper-execution-v1` 的 `spread_bps`、`latency_seconds`、`max_fill_qty`。默认值为0、0、null。价差一半作用于每个买卖方向，并叠加账户滑点；同一证券/供应商时刻共享成交容量，重复抓取不增加容量；部分成交按整笔订单累计计费，撤单只撤剩余数量。该容量是参数化模拟，没有声称还原盘口排队或真实成交量。

## 模型配置与有限循环

实际OpenAI Responses适配器的配置示例（将模型名替换为账户可用的明确标识）：

```json
{"provider":"openai-responses","model":"YOUR_MODEL_ID","key_env":"OPENAI_API_KEY","timeout":30,"max_output_tokens":4096}
```

密钥只从环境读取，JSON文件只存环境变量名称。适配器使用固定HTTPS端点、JSON输出、无工具、`store:false`，本地再次严格验证。网络错误不保存响应正文，模型输出若包含使用中的密钥则拒绝。另支持显式 `https-json` 协议服务；它不是通用的OpenAI兼容端点。

已经准备好真实来源证券资料、每日状态和独立live/CNY账户后：

```powershell
python -m mercury --db data/cn-forward.db cycle --universe 600519.SH --agent data/agent.json --out data/cycle-run-001 --rounds 1 --collect
```

`--collect` 调用固定只读MCP采集工作器，每轮限1至3个SH/SZ标的；此Python工作器不冒充独立LLM子代理。Codex采集子代理也可准备每轮独立capture，用 `--captures <JSON列表>` 交给运行器。连续live轮次不能只重复旧数据库报价。没有自动定时任务或无限后台运行。

每轮保存观察、账户、模型输出、意图、风控、复盘及哈希。故障记录只含固定阶段和错误类型。恢复先查询账本中的意图结果，防止崩溃后重复下单；文件锁防止同一输出目录并发写。下一轮反馈含已发生的订单/成交、拒绝原因和账户结果。

## 评测边界

对齐基准须提供来源、时间、可用时间、相同评测范围和CNY币种；指数是价格比较基准，不注册成可交易股票。缺失不补零。完整交易分组按单标的仓位从零到零计算，未平仓不计入胜率。Sharpe在无法验证完整固定采样序列时返回null与原因，不能用几次快照推断年化能力；当前仅验证完整日采样，日内regular采样明确返回null。详细参数和指标口径见[CLI接口说明](workflow-cli.md)。

应用协议限制远程模型的数据与输出能力，但不能隔离拥有相同用户文件/进程权限的宿主；没有声称实现OS沙箱。供应商哈希只是内容一致性，不是签名。尚无公司行为、幸存者修正、匿名化运行器、大规模历史数据集或长期前向成绩。真实模型API与真实前向成交须分别验证，合成测试不替代它们。
