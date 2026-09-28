# Financial Datasets 认证与采集

本页不存储密钥。Mercury 只使用环境变量 `FINANCIAL_DATASETS_API_KEY`；不要把密钥填入源码、JSON capture、命令行、聊天或 Git。

## Windows 手动填写位置

1. 在 Chrome 打开 [Financial Datasets Dashboard](https://financialdatasets.ai/)，自行登录，在账户的 API key 管理处创建/复制自己的密钥。页面按钮以现场显示为准；agent 不读取或截图密钥。
2. Windows 搜索“编辑账户的环境变量”，进入**用户变量 → 新建**。
3. 变量名填 `FINANCIAL_DATASETS_API_KEY`；变量值填你的密钥。不要改 PATH。只设用户变量，不需要管理员权限。
4. 关闭并重开终端/Codex，让新进程继承环境变量。已有终端也可运行下面的检查脚本，它只从用户环境变量读取到当前进程，不打印值。

```powershell
./scripts/check-market-connection.ps1
```

脚本只做 MCP 初始化和工具发现，不下单、不采集账户隐私。成功显示工具 schema 后，运行 README 的 `collect-quote`。失败保留诊断，不能以 fixture 冒充成功。

## 两条官方 MCP 路径

- 项目内程序：`https://mcp.financialdatasets.ai/api`，`X-API-KEY` 头，值来自环境变量。只读工具白名单，30 秒网络超时，响应大小限制，不跟随重定向。原始响应与工具参数落库，但请求头不入证据。
- Codex 原生 MCP：`https://mcp.financialdatasets.ai/`，官方支持 OAuth。需用户完成浏览器登录。OAuth 登录不会自动提供本地 Python 所需的 API key；这两种认证不能混为一谈。本次不自动改全局 Codex 配置。

子代理可使用已连接的原生 MCP 做研究；用于成交的行情由本地采集路径直接查询并绑定响应，不能由交易 agent 输入任意价格。若将原生工具响应手工导出供离线分析，必须保留 provider/tool/arguments/received_at/result，并标为未认证历史数据，不能导入 live。

## 来源与版本差异（2026-09-27 核查）

- [官方托管 MCP 文档](https://docs.financialdatasets.ai/mcp-server)：报价 `get_stock_price`，历史 `get_stock_prices`。代码运行时先检查 tools/list，不能只凭文档猜 schema。
- [旧官方 stdio 源码](https://github.com/financial-datasets/mcp-server/blob/main/server.py)：名称是 `get_current_stock_price` / `get_historical_stock_prices`，后者只返回 prices 数组，丢失分页外层。当前项目不自动安装或执行该仓库。
- [快照 schema](https://docs.financialdatasets.ai/api/prices/snapshot)：price、ticker、time、time_milliseconds；time 要有时区，若另有毫秒时间须一致。缺时区拒绝，不能用采集时间替代行情时间。
- [历史 schema](https://docs.financialdatasets.ai/api/prices/historical)：OHLCV/time 与分页。当前本地适配仅支持单日日线，其他频率拒绝。

认证准备完成不等于真实连通已确认。首次验证应记录 `mcp-tools` 的 schema、一次真实报价的 quote_id、行情时间和审计结果；密钥值永不进入报告。

2026-09-27 已验证认证与工具发现；真实报价被账户 credits/balance 错误阻塞，详见[真实接入记录](live-integration-2026-09-27.md)。遇到此错误应由账户持有人在 Dashboard 核对额度和数据权益，不要反复请求或自动充值。
