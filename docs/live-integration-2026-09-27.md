# 真实 MCP 接入验证：2026-09-27

本记录补充初次本地审计，不改写此前尚未接入时的历史结论。未保存凭证，未充值、升级套餐或执行交易。

## 已验证

- 用户已手动设置 Windows 用户环境变量 `FINANCIAL_DATASETS_API_KEY`。原进程尚未继承；测试仅将该用户变量读入请求进程的环境。
- 官方 `https://mcp.financialdatasets.ai/api` 的 MCP 初始化与 `tools/list` 成功，返回 28 个工具。
- 实测 `get_stock_price` 必填 `ticker`；`get_stock_prices` 必填 `ticker`，支持可选 `interval`、`interval_multiplier`、`start_date`、`end_date`。本地历史适配仍限单日日线。
- 首次默认 Python 请求标识收到 HTTP 403；使用诚实标识 `User-Agent: mercury-paper/0.1.0` 后初始化与工具发现成功。代码已采用此标识，不改变认证或其他保护。
- 独立采集子代理通过本地 MCP 协议客户端单次调用 AAPL `get_stock_price`，收到响应时间为 `2026-09-27T02:43:10.348903+00:00`。

## 实际行情阻塞

该行情工具返回包含 `credits` 与 `balance` 的普通文本，未设置 MCP `isError`，也没有合法 JSON 行情。无法取得有效价格或行情时间，不能作为交易报价。子代理已停止，没有再调用历史行情，也没有进行模拟成交。

另一次主代理只读 REST 认证诊断返回 HTTP 402，正文分类命中 `insufficient`。REST 结果仅用于诊断，不替代用户要求的 MCP 行情链路；本记录不推断具体余额、套餐价格或可用免费范围。

处理需要账户持有人在 Financial Datasets Dashboard 核对 API credits/balance 或数据访问权益。不得自动充值、升级或绕过服务限制。工具发现成功不等于行情权限已满足。

## 保留的本地证据

以下文件位于 Git 忽略的 `data/captures/`，不进入开源提交；SHA-256 为 capture 内容的规范化 JSON 哈希，不是供应商签名。

| 文件 | capture SHA-256 |
| --- | --- |
| `20260927T024307603626Z-aapl-get_stock_price.json` | `3c5056ab420312eb91e4513da4d5ebd73062d950ba8fb5b9692b2a779d87c2c2` |
| `20260927T024339145721Z-aapl-diagnostic.json` | `cc94ebacd350d107335ed9faaafe94f1e7a08d69d87c6e04061bc57ea6066b72` |

第一份是失败的工具响应，不是有效行情。它在解析前保存，随后验证失败，现保留失败证据。第二份是脱敏诊断。

## 修复与验证边界

市场工具调用现会在返回成功 capture 前解析 JSON；对于没有 `isError` 的 credits/balance 文本，抛出固定的脱敏额度错误。此规则同时覆盖快照和历史工具。请求头与密钥不进入 capture。

新增回归测试检查这类响应不会被当成成功行情，也不回显供应商文本。真实行情计算、真实行情驱动的模拟交易及前向评测仍未完成；本地合成测试不能代替这些证据。

## 本次独立修复复核

未参与实现的审计子代理只读复核 `market.call`、`unwrap` 及新增 billing-text 回归测试。两个价格工具在返回 capture 前调用解析器；没有 `isError` 的非 JSON credits/balance 文本现抛出固定脱敏错误，普通非 JSON 文本同样拒绝。错误消息不拼接供应商正文；capture 仍不含请求头或凭证。未发现该补丁新增的本地正确性或凭证泄漏问题。

实际运行 `python -m unittest discover -s tests -p test_market.py -v`，10/10 测试通过，包括两个工具的无错误标志额度文本拒绝、私有供应商详情不回显及合成密钥不进入错误消息。测试仅使用 mock 网络和合成凭证，不发网络请求、不读取真实凭证；本次复核不独立确认前述真实认证、采集响应或账户权益结论，也不改变真实行情尚未成功的结论。
