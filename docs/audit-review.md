# Mercury 独立审计与修复复核

审计日期：2026-09-27。审计子代理未参与交易、行情接入、CLI、测试或内建审计器实现；只读检查源码，使用临时 SQLite 做独立重现。本报告是唯一由审计代理写入的项目文件，不替代 Codex Auto Review。

## 范围与证据层级

检查 `mercury/engine.py`、`market.py`、`analytics.py`、`audit.py`、`__main__.py` 与测试。覆盖 Decimal 资金算术、买卖预留、并发、撤单、幂等、成本及盈亏、持久化、历史时序、来源标签及只读 MCP 边界。

所有动态证据均为本地合成数据或模拟 MCP 响应。没有真实 Financial Datasets MCP 调用、真实前向实验、券商执行或模型能力证据。未安装依赖。沙箱拒绝临时 SQLite 文件访问后，使用正式权限申请执行临时测试，未绕过自动审核。测试库不属于用户实验数据。

## 初审发现与复核

| 严重度 | 发现及复现 | 修复复核 |
| --- | --- | --- |
| P1 | live 账户可通过公开 `import_quote` 自报 `kind="mcp"`、`source="financial-datasets-mcp"`，证据仅 `{"fixture":true}` 仍成交；内建 audit 返回成功 | 公开导入现拒绝 MCP 标签；`collect_quote` 自建官方 client；存储时核对 envelope、标的、报价、时间、币种。独立重现旧路径已拒绝 |
| P2 | capture 参数标的是 AAPL、响应缺 ticker 时，`historical(capture,"MSFT")` 可错标 MSFT | 现核对请求 ticker，并检查日线间隔、日期范围；独立重现拒绝错标 |
| P2 | `create` 中 SQLite 连接上下文只提交不关闭，Windows 临时库清理发生 WinError 32 | 使用 `closing` 后独立临时测试正常清理，退出码 0 |
| P2 | snapshot 缺 currency 自动补 USD，未记录币种依据 | 缺币种且无明确 instrument contract 现拒绝；操作员显式确认币种保存在证据中。此确认不等于 provider 返回币种 |
| P2 | snapshot/event 相等不能独立验证估值算术 | 新增按事件顺序重建现金、成本、已知行情及预留的快照复算；净值更改并重算 hash 的回归测试仍能发现差异 |
| P2 | `load_history` 两行 close 为 10 和 10.000000001：第二行精度报错，但第一行已提交（quotes count=1） | 改为同一事务批量导入；独立重现失败后 quotes count=0，初始化账本 audit 仍通过 |
| P2 | 收益 report 未同时提供滑点成本及样本起止覆盖 | report 增加 fee/slippage bps、实际滑点成本、起始/截至及首末成交时间，并在同一只读事务取得账户与行情；独立复算滑点为 0.01800000，与结果一致 |

Python 私有方法、同进程 monkeypatch 与本地数据库写权限不是对恶意调用者的安全隔离。本地 SHA-256 仅验证内容一致性，不能独立认证来源或阻止拥有写权限的人重写全部记录。

## 独立算术与并发验证

以 cash=100、fee=5 bps、slippage=2 bps 开始，两线程同时提交每笔数量 6、报价 10 的买单：一笔接受、一笔因可用资金不足拒绝，预留为 60.04200600。撤单后预留为零。

随后在下一条报价 11 买入 6 股，再在下一条报价 12 卖出 2 股，独立预期为：

- 买入成交价 11.00220000，金额 66.01320000，费用 0.03300660，现金 33.95379340。
- 卖出成交价 11.99760000，金额 23.99520000，费用 0.01199760，分摊成本 22.01540220。
- 最终现金 57.93699580，剩余 4 股成本 44.03080440，已实现盈亏 1.96780020，累计费用 0.04500420。

引擎结果与上述 Decimal 复算一致；内建 audit 返回成功。历史同条报价不成交、后续报价成交的独立测试通过。项目测试进一步覆盖重复请求、并发 match、全部卖出、限价、撤单、跳价导致资金不足、陈旧及未来报价和重开账户。

## 实际验证命令

```powershell
python -m unittest discover -s tests -v
python -m mercury --help
```

在全部修复后，独立运行结果为 25/25 测试通过；CLI help 退出码为 0。Windows shell 的输出编码造成 help 中文显示乱码，不影响命令完成。

最终额外独立测试重跑双线程买入预留、撤单、下一条行情买入、部分卖出、两次估值快照与内建审计；现金、成本、费用、盈亏、实际滑点均与前述精确预期一致。历史错误批次原子回滚独立验证通过，临时目录清理正常。本轮未发现尚未修复的 P1/P2 本地正确性缺陷；此结论仅覆盖已检查路径。

额外独立检查通过 PowerShell here-string `@' ... '@ | python -` 执行，使用 `tempfile.TemporaryDirectory()`。来源修复重现如下，均期望 `ValueError`：

```python
t = PaperTrader.create(temp_path, mode="live")
t.import_quote(ticker="AAPL", price="10", asof=utcnow(),
               evidence={"fixture": True}, kind="mcp",
               source="financial-datasets-mcp")
# snapshot: provider 正常 envelope，无 currency 且无 instrument contract
# historical: arguments.ticker=AAPL，调用 ticker=MSFT
```

历史部分导入重现：replay 临时账户，`load_history` 的两行 daily OHLC 分别全部为 `"10"` 和 `"10.000000001"`，两个日期不重复；异常后只读查询 `SELECT COUNT(*) FROM quotes`。

## 未验证及已知限制

- 真实 hosted MCP 端点、账户权限、schema 与响应、真实市场时间戳均未运行验证；模拟 transport 测试不代表真实接入成功。
- 币种缺失时依赖操作者的 instrument contract，尚无独立标的元数据查询来证明币种。
- 历史 EOD 下一 UTC 午夜的可用性约定是简化模型；未实现逐版本 point-in-time 数据、公司行为、复权、完整交易日历、订单簿、成交量约束或严格幸存者偏差控制。
- 测试证明本地模拟规则与账本一致，不证明策略或模型真实收益；没有严格基准评测结论。
- 对恶意同进程代码及任意数据库写入不提供安全隔离。若未来需要该边界，应采用不同身份、进程与权限，而不是把私有方法当安全机制。
