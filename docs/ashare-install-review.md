# ashare-mcp 隔离安装与真实 MCP 验证

日期：2026-09-27。上游：[CharmYue/ashare-mcp](https://github.com/CharmYue/ashare-mcp)。固定 commit：`614b380606e18eaf35747a0c3b2f27ec551caaa3`。

安装及证据根目录：`data/integrations/ashare-mcp-20260927-105153/`。没有修改上游源码、全局 Codex 配置或系统 Python，没有公开 HTTP 端口、建立隧道、提交或推送。

## 来源与安装行为检查

- GitHub 固定 commit 源码压缩包 SHA-256：`67c79a1ae890b1c83936e96a8a6eb8595e3503e71d57a7f1e6cc877d3484577d`。
- 保留原始 `upstream/pyproject.toml` 和 `upstream/uv.lock`。lock 包含 112 项（包括可选包、不同平台选择）；Windows Python 3.13.14 的生产运行环境安装 96 项。
- 固定直接依赖：fastmcp 3.3.1、akshare 1.18.64、baostock 0.9.1、pandas 3.0.3、loguru 0.7.3、pydantic-settings 2.14.1、tenacity 9.1.4。直接依赖及 jsonpath 的 PyPI 版本元数据已保存到 `dependency-review/`，其中锁内发行文件 SHA-256 均与 PyPI 元数据一致。完整安装清单在 `installed-packages.json`。
- 先用 `uv sync --frozen --no-dev --no-default-groups --no-install-project --no-install-package jsonpath --no-build --python C:/Users/Logan/dev/python313/python.exe --no-python-downloads` 安装 95 个固定 wheel。UV_CACHE_DIR 和 UV_PROJECT_ENVIRONMENT 均指向本次新目录。
- 唯一无 wheel 的生产包 jsonpath 0.82.2：PyPI 发行文件下载多次 TLS EOF；从 PyPI 元数据列明作者原站的 HTTPS 下载同一源码发行，SHA-256 `d87ef2bcbcded68ee96bc34c1809b69457ecec9b0c4dd471658a12bd391002d1` 与 PyPI 和 lock 一致。审查其 setup.py、setup 导入模块后，使用隔离 uv pip 安装本地 sdist。setup 仅声明纯 Python 模块，模块顶层没有网络或子进程行为；库内表达式求值能力不能用于执行行情里的任意指令。此步骤的临时打包后端由 uv 解析为 setuptools 84.0.0，官方元数据随后保存在 dependency-review；原上游锁未覆盖这个构建依赖，不能将其称为完全锁定的构建工具链。
- 上游入口为 `ashare_mcp.server:main`。本地使用 `python -m ashare_mcp.server --transport stdio` 和 PYTHONPATH 指向固定源码，避免另行运行上游项目构建脚本；不是 PyPI 的 ashare-mcp 包安装。
- 已审查 server/config/cache、market 工具和各 data_sources。没有发现券商下单或资金接口；网络行为为 akshare 研究/行情调用及 baostock 只读数据登录/查询。上游 HTTP 分支存在，但本次未启动。
- 上游 `.env` 读取当前工作目录；源码目录只有 `.env.example`。启动器只传递系统环境白名单，不传递 Financial Datasets、Tushare 或其他 API 密钥。
- 上游默认缓存路径在用户家目录，SQLite 值使用 pickle；因此启动器强制使用本安装根下每次新建的运行目录，避免复用其他环境可篡改的 pickle 缓存。日志会轮转并保留 7 天，因此原始 MCP 证据独立保存，不能依赖运行日志作为唯一证据。

## 可调用方式

在 Mercury 根目录运行：

```powershell
python data/integrations/ashare-mcp-20260927-105153/launch_stdio.py
```

这是 stdio MCP 子进程入口，stdout 专供 JSON-RPC。启动器使用隔离 venv，并创建新缓存目录。安装阶段尚未注册到全局 Codex；后续状态见文末。

## 真实 stdio 验证与字段约束

原始完整 MCP 响应、调用参数、UTC 采集开始/结束时间及 JSON SHA-256 保存于 `probe-20260927T030223Z/`，工具列表原始 schema 为 `tools.json`。这里保存的是完整解析后的 MCP 响应，未声称保留 provider HTTP 的原始字节。

initialize 和 list_tools 成功，实际发现 30 个工具。各行情工具各调用一次，无额外批量采集：

用户提供的交易日历信息：2026-09-25 至 2026-09-27 为中秋假期，A 股休市。因此日线窗口 09-24 至 09-25 仅返回 09-24，符合该交易日历的预期，不能将休市日未返回记录描述为数据缺口。本次未追加交易日历网络查询。

| 工具 | 实际输入 | 实际结果 | 时间与来源约束 |
| --- | --- | --- | --- |
| get_daily_kline | 600519，2026-09-24 至 2026-09-25，adjust 空字符串（未复权） | ok:true，source:akshare，count:1；仅 2026-09-24，close:1237.0；09-25 为休市日，返回覆盖符合上述交易日历预期 | date 是交易日粒度；没有真实行情时刻或可用时间，不得当实时交易报价 |
| get_minute_kline | 600519，5 分钟，2026-09-25 14:45 至 15:00，未复权 | ok:false，data_source_unavailable，ProxyError；实际为连接错误，不能用假期解释 | 查询日期为休市日，但实际返回未到达有效数据响应；没有成功样本，分钟 time 字段仅经源码检查 |
| get_realtime_quote | symbols:[600519] | ok:false，data_source_unavailable，ProxyError；实际为连接错误，不能用假期解释 | 没有成功报价；源码成功分支无可靠行情 timestamp，采集时间不能替代 |
| get_stock_info | symbol:600519 | ok:false，data_source_unavailable，ProxyError；实际为连接错误，不能用假期解释 | 没有成功样本；上市日期不是报价时间 |

四个 MCP 返回均 `isError:false`，业务失败须额外读取 `structuredContent.ok` 或解析 content 中 JSON 的 `ok`。不能只看传输成功。

实际 schema：quote 要求 symbols 字符串列表；daily 要求 symbol/start_date/end_date，adjust 为 qfq/hfq/空（默认 qfq）；minute 要求 symbol，period 为 1/5/15/30/60（默认5），日期默认空，adjust 默认qfq；info 要求 symbol。四者 outputSchema 都只有宽泛的 object，没有保证时间、货币或价格字段。

source 为上游自述的适配器来源标签，不是逐条 HTTP 源响应证明。上游未保证 currency 字段；A 股人民币约束必须由 Mercury 标的/市场契约处理。daily/minute 的前复权默认值不适合作实际模拟成交价格，接入时应显式要求未复权，并校验数据粒度、缺失、日期与估值限制。

结论：Windows 隔离 stdio 服务与工具发现已实际成功；真实日线样本为一个交易日，09-24 至 09-25 的返回符合用户提供的中秋休市日历预期，样本规模仍有限；真实 quote/minute/info 接入的实际结果仍为网络连接错误，不能归因于休市；真实前向成交与模型评测未验证。没有将任何失败返回、缺时间数据或少量日线宣称为实时模拟交易成功。

## 后续集成验证（2026-09-27）

已备份用户 Codex 配置并注册 `mercury-ashare`，入口为 `scripts/run_ashare_mcp.py`；启动前验证固定提交、来源文件清单和启动器哈希，子进程仅继承允许的系统环境变量。Mercury 适配桥实际发现 30 个工具。当前聊天是否已热加载原生 MCP 工具尚未确认，新聊天或重载可能必要。

原始日线响应经本地正常化和分析得到一条 2026-09-24 记录，没有追加网络请求。另行运行的合成 CNY CLI 流程验证了买入、手续费、同日不可卖及账本审计；它与真实日线证据分开标记，不能证明真实前向成交。私有验证记录：`reports/ashare-validation-20260927T032632707825Z.json`。独立审计最终 58 项本地测试通过，详见 [审计报告](ashare-audit-review.md)。
