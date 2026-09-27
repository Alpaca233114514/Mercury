# A股流程独立审计（2026-09-27）

审计者未参与实现。只读检查实现与账本；验证仅使用内存或新临时目录，未操作用户实验数据库、历史账本、真实凭证或网络。独立审计不替代 Codex Auto Review。

最终结果：四项 P2 已由实现者修复并通过独立原始反例复核。包括末轮修复的稳定版本独立运行全部 **145 项测试通过**，`python -m mercury --help` 退出码 0。本次范围内未留下已确认未修复的 P1/P2；本轮新真实接入与前向评测仍未验证。

## 初审发现

1. **P2：live 账户估值越过 Observation cutoff。** `workflow.public_account` 允许当前账户时间比 cutoff 晚五秒，调用的 `engine.portfolio/_quote` 按当前时间取估值。合成临时账户在 cutoff 的价格为 10，随后一秒报价为 11；cutoff 后三秒取得的模型账户投影含 100 股市值 1100，尽管 Observation 末价仍为 10。账户市值/权益间接泄漏 cutoff 后价格。复现使用临时 DB 显式模拟 ingress；不是真实 MCP 证据。
2. **P2：episode 复用已有账户未核对统一配置。** `_account` 对已有合法 DB 仅做账本审计，无 seed receipt、无订单时接着按新 episode 种子，未核对已有初始资金、费用、模式及政策。临时目录预建 `agent-<digest(probe)[:16]>.db`，初始现金 200000，episode 指定 100000；`compare_agents` 成功，报告顶层 `initial_cash=100000`，账户 `initial_cash=200000`。统一资金契约失效。
3. **P2：完整合成演示尚无成交。** 初次 `python -m mercury workflow-demo` 输出两个模型均 `refused_cycles=1`、`fills=0`。首次决策只有两条行情，momentum window=2 需要三条；后续报价 11 等于当日涨停，买入按正确的无盘口规则无法成交。拒绝机制正确，但此示例不能验收完整买入/退出流程。
4. **P2：regular Sharpe 错误接纳非交易时段。** `sharpe_for_schedule` 只检查等间隔及 `252*14400/gap` 年化，未检查交易日/时段。21 个周六每小时估值被年化成 Sharpe 346.2988261706355，无 null reason。

各项已交主代理修复；复核结果见下节。初始失败记录保留，用于区分修复前后的证据。

## 初审验证证据

- 独立运行 `python -m unittest discover -s tests -v`：当时 102 项通过。实现随后继续变动，此结果不代表最终版本。
- 部分成交独立 Decimal 复算：200 股、每股 10、容量 60，四笔 60/60/60/20。按配置转让费 0.1 bps、佣金最低 5 的订单累计费用为 5.02；现金 97994.98、持仓成本 2005.02、持仓 200。与账本一致，`audit` 通过。复算直接使用费率和 `ROUND_HALF_UP`，未调用被审实现的费用函数。
- 受风控约束的并发与幂等：五个独立 400 股意图，每个重试一次，仓位上限 0.2；四个唯一订单获准，一个拒绝。并发撮合仅四笔成交，持仓 1600、现金 83979.84、成本 16020.16，与独立算术一致，`audit` 通过。
- 静态检查确认 JSON 重复键、非有限数、未知字段及无效引用会拒绝；模型无执行工具，HTTP 重定向关闭，凭证按需从环境读取，输入/输出包含凭证时拒绝，失败日志使用固定诊断。未找到 eval/exec 或原始未来 capture 直接进入公开 Observation 的路径。

Sharpe 最小复现（纯内存）：

```python
from datetime import datetime, timedelta, timezone
from mercury.analytics import sharpe_for_schedule
start = datetime(2026, 9, 26, tzinfo=timezone.utc)
points = [{"time": (start + timedelta(hours=i)).isoformat(),
           "equity": str(100000 + i * 100 + i * i)} for i in range(21)]
print(sharpe_for_schedule(points, {"frequency": "regular",
    "expected_times": [p["time"] for p in points],
    "periods_per_year": 1008, "min_returns": 20}))
```

账户复用最小复现（新临时目录）：

```python
import tempfile
from pathlib import Path
from mercury.common import digest
from mercury.engine import PaperTrader
from mercury.examples import synthetic_episode, offline_template
from mercury.evaluation import compare_agents
from mercury.models import OfflineAgent
spec = synthetic_episode()
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / ("agent-" + digest("probe")[:16] + ".db")
    PaperTrader.create(path, cash="200000", market="CN", mode="replay",
        start=spec["events"][0]["time"], calendar=spec["calendar"],
        fee_bps=spec["fee_bps"], slippage_bps=spec["slippage_bps"],
        minimum_commission=spec["minimum_commission"], risk_policy=spec["risk_policy"])
    result = compare_agents(spec, {"probe": OfflineAgent(offline_template())}, output=folder)
    print(result["initial_cash"], result["models"]["probe"]["report"]["portfolio"]["initial_cash"])
```

## 复核与限制

1. **冻结边界已修复。** `mercury/workflow.py:117` 的账户投影在 `:166` 用冻结行情重算市值，`:174` 记录估值 cutoff，保留实际账户 snapshot time。重跑同一 10→11 后 cutoff 报价反例，公开市值现为 **1000.00000000**，没有取 1100。
2. **已有账户契约已修复。** `mercury/evaluation.py:48`，其中 `:68` 开始核对 CNY/replay、初始现金、费用、滑点、max_age、初始化时间、完整市场配置、风险及撮合政策。原先 200000 账户/100000 episode 反例现拒绝，错误为 `existing episode account initialization/policies differ from frozen comparison contract`；等价部分种子中断恢复仍通过。
3. **完整合成演示已修复。** `mercury/examples.py:21` 增加起始信号行情，第二交易日参考价对应前日价格。独立 `compare_agents` 现为 **2 fills、1 closed group、0 refused cycles**，重复恢复报告相同。0.1 配置成交 900 股，买入 10.01、次交易日卖出 11；独立算术为 `900*(11-10.01)-15.14=875.86`，最终现金 **100875.86**，与报告相符。此收益只属于人工合成行情。
4. **日内 Sharpe 已停止错误年化。** `mercury/analytics.py:57` 在缺少可信交易时段采样网格时明确返回 null。原周六每小时反例现 `annualized_sharpe=None`，原因为 `intraday Sharpe unavailable: verified trading-session sampling grid not implemented`。日线仍要求完整 CN 日历、相同取样时间及至少 20 个收益样本。
5. **日基准审计覆盖已补强。** `mercury/audit.py` 的 `_replay_risk` 现按前日 snapshot/risk valuation、现金账户或缺失基准独立重建。临时账户同时篡改 `risk_days` 与所有 check 的基准并同步重算全事件链后，审计仍报告 `daily baseline differs from independent prior valuation replay`；未用修改历史账本的方式取得通过结果。
6. **慢模型保持冻结时点。** `mercury/risk.py:104` 在 live 提交时以账户 max_age 与意图 quote TTL 的较小值约束观察年龄，replay 仍要求同一 clock；既有经济状态哈希核对、提交报价有效期、成交时到期/陈旧拒绝保留。稳定全套覆盖 6 到 30 秒的合成模型等待、到期未触价订单释放、较短 quote TTL、共享容量、部分撤单及旧账本不迁移。

## 末轮增量复核

- **同事务时间一致性。** 风控检查与组合估值用同次撮合的权威 `now`，风险记录与 fill timestamp 一致。新增递增模拟时钟每次前进 1ms 的合成回归通过；这不是实际 live MCP 成交。
- **挂单防重复。** `mercury/strategy.py:222` 读取公开 `remaining_qty`，有未完成挂单时在 `:235` 保守持有，保留原因，不另建相同目标订单。新 Observation 未成交及部分成交剩余量回归均通过。
- **恢复绑定。** cycle manifest 的 `previous_review_sha256`（`mercury/workflow.py:186`）与六份阶段引用绑定；缓存回执核对持久化 intent/risk 与当前账本意图结果。模型身份包含 timeout 与 `max_output_tokens`（`mercury/models.py:144`），参数改变不复用旧运行身份。
- **每轮自动审账。** 独立审计末轮发现新收据发布前没有执行自动账本一致性检查，主代理同意并交实现者修复。`checked_audit`（`mercury/workflow.py:67`）仅返回成功摘要；新收据前 `:239`、缓存返回前 `:204` 均执行只读完整 audit，失败保留固定 `failure-audit`、拒绝收尾。成功收据包含复算现金、检查数量、事件头与本地一致性限制。临时 DB 损坏后恢复拒绝、失败无新收据、修复模拟故障后幂等恢复回归均通过。

最终复核命令：`python -m unittest discover -s tests -v`（**145 tests，16.917s，OK**）；`python -m mercury --help`（退出码 0）。末轮未重复已关闭的完整旧反例；全套覆盖新增边界。所有需要建立临时账户的命令均通过正式 escalated 审核执行，没有改保护或绕过 Auto Review。

## 主代理持久证据只读复核

`data/validation/workflow-root-20260927T060453Z-52610d/comparison`：独立直接读取两份 SQLite 账本，按价格、数量及配置费率重新计算费用与现金，没有复用实现的计费函数。offline-a 为 2 fills、费用 **15.14**、现金 **100875.86**、平仓；offline-b 为 2 fills、费用 **22.83**、现金 **101858.17**、平仓。与持久账本、hash 校验过的报告一致，当前只读 `audit` 均通过。

`data/validation/workflow-real-saved-20260927T060705Z-190b95`：只读校验已保存阶段文件和 proof 的 SHA，quote asof 为 **2026-09-24T08:11:44+00:00**，Observation cutoff 为 **2026-09-27T06:07:05.944526+00:00**，日历休市，0 orders、holding。记录明确 `new_network_call=false`、`real_model_api=false`；只证明已保存真实行情进入休市持有/拒绝制造成交的路径，不证明本轮实时采集、真实模型决策或前向收益。

最终自动审账版本新证据 `data/validation/workflow-final-20260927T061428Z-95409f`：只读验证 `root-final-review.json`、最终 comparison report 及四份 cycle receipt 的 SHA；四份 receipt 均含 `audit.ok=true`，全部六项阶段引用哈希匹配。两 agent 仍各 2 fills，现金/费用分别为 100875.86/15.14 与 101858.17/22.83；主代理 proof 明确纯合成、无网络/模型调用、恢复一致。旧证据未覆盖或改写。

本轮新 ashare/FDM MCP 接入、真实模型 API、真实前向收益、操作系统权限隔离、其他历史用户数据库均未验证。成交算术结论仅适用于明确标注的本地合成模拟；已保存真实行情另按上节边界报告。来源真实性、公司行为/幸存者偏差与真实市场微观结构不由这些测试证明；日内 Sharpe 网格尚未实现，已明确留空。
