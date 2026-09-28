# A 股受控流程与评测接口

模型交易和实际计算已支持自动记录、只读查询及 JSON/CSV 导出，详见 [记录程序说明](records.md)。旧账户须先通过 `records upgrade --backup ...` 备份并增量启用，才能开始新的模型 cycle。

此流程仅本地 CNY 假钱；没有券商、充值或钱包接口。执行价格、费用、风险与持仓始终由 PaperTrader 计算。外部模型只接收冻结 JSON 和公开账户约束，不提供 Python、shell、SQL、文件或任意工具执行功能。该数据协议不限制拥有宿主/数据库写权限的管理员，也不构成操作系统隔离。

## 完整离线合成示例

```powershell
python -m mercury workflow-demo
python -m mercury workflow-example --out data/example-cn-001
python -m mercury backtest --episode data/example-cn-001/episode.json --agent data/example-cn-001/agent.json --out data/backtest-cn-001
python -m mercury compare --episode data/example-cn-001/episode.json --agents data/example-cn-001/agents.json --out data/compare-cn-001
```

示例仅使用明确标记 synthetic 的分钟时点 fixture、逐日资料和合法连续竞价时段。9 月 24 日买入、9 月 28 日 T+1 解锁后卖出；历史订单仅在下一条行情成交。示例收益不说明策略或模型真实能力。默认例子无价差/延迟/容量限制；显式 execution_policy 可启用参数化模拟，不能称真实微观结构校准。

episode.json 中包含独立初始资金、日历、证券、逐日状态、fixture quotes 或原始 history_captures、严格递增 events、风险/费用/执行设置、可选研究 documents、sampling 和 benchmark。事件 time 推进 replay 时钟，decide 控制是否运行模型 cycle；不会改 live 时钟。历史日线保持实际 session-close 约定，不能悄悄提前到可成交时段。数据不满足撮合规则时只保留拒绝/挂单。

compare 建立独立账户并共享同一个市场观察 JSON；行情 ID、股票池、时钟、初始 CNY、规则及费用完全相同。账户上下文和已发生反馈可以因模型行为不同而不同。历史文件由可信运行器持有；拥有文件权限的宿主仍能读未来数据，模型的预训练记忆、公司行为和幸存者偏差没有消除。

## 当前账户 cycle

```powershell
python -m mercury --db data/new-cn.db init --market CN --mode live --calendar config/cn-calendar-2026.json --risk-policy data/risk.json --execution-policy data/execution.json
python -m mercury --db data/new-cn.db observation --universe 600519.SH --out data/observation-cn-001.json
python -m mercury --db data/new-cn.db cycle --universe 600519.SH --agent data/agent.json --rounds 1 --collect --out data/cycle-cn-001
```

必须先注册有来源的当前证券和逐日状态；没有资料不能靠 cycle 猜测。固定 --collect 是确定性只读采集 worker，不是独立 GPT 子代理；live 每轮最多一次 bounded ashare timestamp 调用（1–3 标的）。亦可传 --captures 指向独立采集子代理交付的新 capture 列表，每轮一份。live 没有本轮新采集输入会停止；采集失败或过期报价不能制造成交。实际网络和模型调用须有相应授权、密钥/额度；本次实现验证不自动调用付费模型或新增实时行情。

cycle 阶段：冻结 Observation/账户 → 研究/策略 JSON → 严格校验 → 本地 DSL 编译单 TradeIntent envelope → 确定性风险 → PaperTrader 撮合 → 复盘。保存原模型身份、观察、账户、模型输出、意图、风险与执行反馈。模型能看到公开风险限额及拒绝原因，从已发生结果重新规划。默认没有后台服务或定时任务；rounds 有界，replay 多轮市场推进由 episode events 完成。

产物 create-only，以内容哈希绑定。恢复先检查阶段文件、manifest、已落库 intent 和经济状态；相同意图不重复下单。写入采用完整文件后原子发布。错误阶段只保存固定脱敏原因和类型，不保存 HTTP 错误体、密钥或未知模型文本。更换模型/数据/账户需要新的输出身份；同输出恢复返回原历史收据，不表示新一轮完成。

每轮收尾和已有收据恢复都执行只读账本一致性检查；失败留存 audit 阶段记录并拒绝收尾，成功新收据保存复算现金、检查数量和账本链头摘要。这是本地程序检查，不替代未参与实现的子代理独立审计。

## 模型驱动

离线配置由 workflow-example 生成。真实 GPT 配置必须显式填写 model，不推测账户可用模型：

```json
{"provider":"openai-responses","model":"USER_SELECTED_MODEL","key_env":"OPENAI_API_KEY","timeout":30,"max_output_tokens":4096}
```

OpenAI 驱动使用官方 Responses HTTPS endpoint、store:false、tools:[]、tool_choice:none 和 JSON mode；严格 schema 校验在本地完成。协议核对依据：[结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs)、[Responses API](https://developers.openai.com/api/reference/typescript/resources/beta/subresources/responses/methods/create)。只发送必要公开观察、账户和反馈。Incomplete、refusal、工具调用、重复 JSON key、非有限数字和已知密钥回显均拒绝，成功响应中发现密钥也不会落盘。HTTP 错误不记录响应体。没有自动重试或代码执行。

模型输出 {research,strategy}；研究/策略正文引用 Observation 资料 ID。observation_id/as_of 如省略由本地绑定，模型显式提供必须完全相同。research_id/strategy_id 在本地计算，不要求模型手算 SHA-256。策略仅可使用 momentum/sma/volatility 受限信号、显式参数、equal 权重、再平衡/持有期/失效条件；不能指定成交价、文件路径或风险政策。

保留 generic https-json 驱动用于用户自己的兼容服务，配置需 provider/endpoint/model/key_env；它不是任意服务自动兼容或 OpenAI API 的别名。环境密钥只在请求时读取，不进入配置产物或模型输入。本次只用 mock 核对 HTTP 协议；真实付费 API、真实前向策略收益尚未验证。

## 指标与基准定义

- turnover：执行买卖名义金额总和 / 初始资金，不年化；披露这一分母约定。
- round_trip_win_rate：同标的持仓从零到零的完整 position episode 净盈亏比例，包含全部部分成交和费用；未平仓组不计胜率。保留旧 profitable_sell_fill_fraction，但不把它称为完整往返胜率。
- drawdown：账户报告只覆盖记录观察点；缺失估值保留 null。
- Sharpe：默认 null 并说明原因。daily 需要完整 CN 交易日历、每日同一采样时刻、至少 20 个收益样本；不能把分钟序列声明成 daily。regular 暂不计算：尚无可信交易日/日内时段采样网格，周末或夜间等间隔点不能靠频率参数获得年化值。daily 通过形式约束仍不代表统计充分性证明。
- benchmark：显式 CNY 对齐价格序列，记录来源/hash/availability、覆盖、费用和分红口径；缺失、迟到或覆盖不足返回 null。index-price-return 从不注册为股票下单。buy-hold-price-return 也只是外部价格序列，并非已按同费用和撮合规则交易的基准账户。没有硬编码指数或声称已有真实指数数据。

本地测试和合成示例只验证代码行为；真实数据接入、付费模型 API、真实前向模拟表现分别报告。
