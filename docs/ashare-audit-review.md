# A 股本地模拟独立审计

日期：2026-09-27。审计代理未参与实现，仅检查代码、规则配置、来源说明和临时合成数据库；本报告不替代 Codex Auto Review。不联网、不读取真实凭证、不执行真实交易，不修改源码或用户实验账本。

## 已复现发现

| 严重度 | 发现 | 证据与修复要求 |
| --- | --- | --- |
| P1 | `available_at` 未规范化，SQL/快照审计按字符串比较带时区时间 | 报价 asof=2026-09-24T10:00:30+08:00，available_at=2026-09-23T23:05:30-03:00（实际北京时间10:05:30），模拟北京时间10:01即以10.51成交，audit成功。入库统一UTC并拒绝无时区/非法值；成交审计须独立检查可用时间 |
| P1 | 证券和每日状态来源只要求非空，缺乏身份与时点绑定 | source.symbol=WRONG、source.day=2026-10-01、published_at=2026-10-01T00:00:00Z 的资料仍用于9/24下单成交，audit成功。应明确身份、适用日期/区间及资料可用时间，禁止未来发布资料回填 |
| P2 | DAY 过期只在 submit/match 中处理，单纯 advance 后资金仍冻结 | 9/24挂买单100股、限价10，advance到9/28；orders仍open，portfolio.reserved_cash=1005.01，audit成功。须确保时钟推进/账户读取后的有效预留符合过期规则 |
| P2 | `cn_audit` 未复算风险股50万累计申报 | 自己新建合成临时库中，已有合法500000股挂单，再插入配套submit事件的100股挂单，现金和hash一致，audit仍成功。审计须复算当日已成交买入与待成交量 |
| P2 | `cn_audit` 不拒绝同日盘中过早过期 | 自己新建合成临时库在10:00创建挂单并构造同一时刻expire，配套状态和hash一致，audit成功。审计須核验跨日或收盘后条件 |

上述非正常账本测试仅用于审计器语义校验，未修改任何用户或历史实验账本。普通下单入口的ST累计限制已实现；审计遗漏不表示该入口能够直接超额。

## 实际检查与验证

已读取 `AGENTS.md`、`docs/ashare-rules-sources.md`、`mercury/cn.py`、`cn_audit.py`、`engine.py`、`audit.py`、`analytics.py`、`__main__.py`、`config/cn-rules.json`、`cn-calendar-2026.json` 与 `tests/test_cn.py`。三所规则来源核实采用项目已有研究记录，本审计未重新访问官方网页，不独立背书所有规则条款。

运行命令：

```powershell
python -m unittest discover -s tests -v
```

初审时实际结果为53/53通过，包含旧USD路径和新ashare适配本地测试；以上独立失败重现不在当时测试覆盖中。重现使用 PowerShell here-string `@' ... '@ | python -`、标准库 `tempfile.TemporaryDirectory()` 及临时SQLite，沙箱需权限时走正式审核。

可复现时序案例：创建CN/CNY账户，时间9/24 10:00+08，注册SH_MAIN_STOCK证券与9/24状态，初始价10；挂100股买单、限价11；再导入asof 10:00:30、价10.5且available_at如表首行，advance 10:01后match。来源案例将资料source设置为表第二行。DAY案例在同库再挂100股、限价10，advance 9/28后直接portfolio，不调用match。

审计器负例通过在自己新建临时库构造`orders`与对应`submit`/`expire`事件测试，事件使用项目canonical/hash规则生成；保留现金充足且其他字段一致，使结果不依赖普通hash破坏。

## 证据边界

- CNY账户与USD账户分离，旧账户未执行schema迁移；既有USD测试通过，但不等于覆盖所有外部历史数据库版本。
- 日历明确排除9/25–27中秋休市，9/24股票T+1解锁为9/28；国庆下一交易日规则由显式交易日列表决定。
- 连续竞价限价模拟未实现集合竞价、价格笼子、订单簿、真实成交量、临时停市、部分成交、实际券商逐单佣金汇总、公司行为或严格前向基准；限制必须保留。
- 证券类别、ETF回转资格、停牌、风险警示与上市阶段不能从代码前缀、名称或走势推断。来源hash和显式分类只是本地一致性，不能证明供应商认证或现实正确性。
- 本次没有真实ashare MCP数据或真实前向交易证据。通过合成测试不能当作真实市场规则完整性、策略收益或模型能力证明。

## 修复复核

五项初审发现已修复，独立复跑原重现条件确认：

- 非UTC偏移的 `available_at` 规范化为UTC，10:01不能读取实际10:05:30才可用的报价；成交审计新增明确可用时间检查。
- 来源须绑定 exact symbol、reference、available_at，每日资料须绑定 day；错身份资料拒绝，未来资料虽可留存但不能下单；live模式拒绝显式 synthetic 资料。
- CN advance 自动使跨日DAY订单过期并释放预留，直接读取 portfolio 的预留为零；未改写已有fills。
- 合成异常账本的500100股风险股申报现触发审计 quota 错误。
- 合成异常账本的同日10:00过早 expire 现触发 DAY validity 错误。

实际独立标准验证为55/55通过，`python -m mercury --help`退出码0。未安装依赖或调用网络。

追加只读检查 `mercury/ashare.py` 与 `scripts/ashare_mcp_call.py`：工具限定只读白名单，K线必须显式 `adjust=""`；保留raw及provider时间，分钟可用时间按周期延后且标注bar-end不确定；原子批量导入使用同一事务。子进程环境按白名单构造，不转发API key，stderr不进入capture；helper的异常仅给类型，不回显正文。日线15:00报价不属于支持的连续撮合时段，因此目前日线可做研究/估值，不能声称已支持日线成交回放。

新增两项待主代理处理/明确的接入问题：runtime只比较config.commit字符串却不核launcher内容，helper也未核commit；capture的固定commit字段本身不能证明实际运行版本。另snapshot的缺失currency目前默认CNY，且实时来源规范字段不及历史路径完整；应提供明确币种契约/规范来源记录或继续拒绝。当前上游无完整实时timestamp已fail-closed，未有真实行情成交证据。

## 第二轮新增资金边界发现

P1：卖出金额低于最低佣金时可发生负现金。独立临时合成IPO无涨跌幅场景：初始现金6 CNY、滑点0，9/24买入100股、价格0.01，金额1、佣金5，现金归零；9/28 T+1解锁后卖出100股、同价，金额1、费用5，撮合完成且现金变成-4。内建审计随后发现negative cash，但非法成交已经落库。应在卖出撮合前检查现金加净卖出所得仍非负且覆盖其他挂买单预留；不足时拒绝成交，不减少实际费用或制造负债。该反例使用显式身份/日期/可用时间的synthetic来源与有效规则时段，不依赖前五项已修复漏洞。

接入复核补充：即使给snapshot正确CNY契约，响应顶层明确currency=USD而行内缺币种时仍返回CNY；这是实际纯函数重现，不只是字段缺失建议。主代理已新增顶层非CNY及历史行内非CNY拒绝，待最后测试确认。另原CLI `import-fixture` 未传币种，CN账户总使用USD默认值而拒绝，已报告待处理。

## 最终本地复核结果

第二轮资金反例重跑后，低金额卖单现被拒绝，现金仍为0、100股持仓保留、审计通过；未截断费用，也未生成卖出fill。撮合同时保护其他挂买单预留。明确CNY的fixture通过实际CLI parser/run路径成功导入，optional currency现已支持；原接口缺口已解决。

实时响应顶层USD的纯函数重现现被拒绝；币种真正缺失时必须提供registered CN instrument契约，不再静默补CNY。capture已补market、adjustment、frequency、rules_version、source_sha256和runtime_identity。

runtime新增launcher与source manifest哈希、源码清单覆盖、commit receipt以及路径边界检查，helper在启动服务前再次验证；本地测试确认launcher或已审查源码变动即拒绝。这里仍是受信任本地配置/收据的一致性验证，不能当作供应商签名，也不对拥有整个运行目录和配置写权限的恶意同进程代码提供安全隔离。

独立最终运行 `python -m unittest discover -s tests -v` 为58/58通过；最后自定义临时脚本额外验证CN fixture CLI、最低卖出佣金不负债、顶层错误币种拒绝，全部通过。已检查路径未留下未修复的P1/P2本地正确性发现。真实MCP启动和行情采集由另一个工作流验证，本审计没有网络、凭证或真实市场执行证据。

## 最大回撤口径最后复核

最后发现并修复ashare历史分析将回撤表示为负数、与账户/FDM正幅度不一致的问题。独立用所有有先后顺序的价格对穷举峰值到低点损失率，不调用实现算法求预期；多峰 `[10,15,12,20,10,18]` 为0.5，上涨 `[10,12,15,20]` 为0，下跌 `[20,18,10,5]` 为0.75，持平 `[10,10,10]` 为0。四组结果均与修复后的ashare分析及`analytics.drawdown`一致。

合成日期只使用9/17、18、21、22、23、24有效工作交易日，未用9/25–27中秋休市行情；新增一致性断言的两日数据为9/23–24。最后再次运行全套标准命令，58/58通过，未发现本次小改的剩余问题。这是本地统计与合成测试证据，不是实际行情、成交或真实前向收益证据。

## 宿主路由继承追加审计

只读比较隔离安装目录的新旧launcher，network_v2仅新增HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、NO_PROXY四个允许变量，保留原launcher及上游源码；native bridge、SDK helper与stdio入口复用相同允许集合。代码未修改系统代理设置、TLS校验或沙箱权限；路由原样继承不代表取得超出宿主授权的网络权限。本次没有网络验证，真实网络检查仍须走正式权限与自动审核。

初次本地ashare回归19/19通过，但独立合成反例揭示凭证边界：HTTPS_PROXY带userinfo，mock上游错误detail回显完整URI时，capture包含合成用户名/密码。上游错误wrapper会把异常正文放进detail；仅测试无认证localhost代理无法证明代理凭证不会进入capture。测试只输出泄漏布尔值，未访问真实凭证或联网。主代理拟在启动前固定脱敏拒绝认证代理，不静默丢弃代理/绕开路由；无凭证代理及NO_PROXY继续原样继承，待修复复核。

该凭证边界已修复并独立复核：共享`subprocess_env`用于native bridge、helper及stdio入口，当前注册的network_v3 launcher也进行同等启动前检查。无认证HTTP/HTTPS/ALL代理和NO_PROXY原样传递，API key与无关token不传入；认证proxy userinfo及不能解析的URI均抛固定脱敏错误，不输出URI、不启动下游进程、不静默改用其他路由。独立验证三类大小写变量乘三种认证/编码/错误URI共9个反例均在spawn前拒绝，正常四路由变量保持值与大小写不变。

只读`verify_runtime`确认当前network_v3的launcher、manifest、commit收据和源码清单一致；没有修改原launcher、ACL、TLS或系统网络配置。完整标准测试59/59通过。本轮未运行网络；实际MCP仍存在RemoteDisconnected的状态由采集工作流报告，不能把宿主单接口HTTP200或这次回归通过称为真实MCP采集成功。9/25–27休市没有新行情不算数据缺失，而连接错误也不能归因于休市。

## 供应商时间戳扩展与成功采集证据复核

本轮只读审查 `mercury/eastmoney.py`、`scripts/ashare_timestamp_server.py`、来源审查文档及bridge/runtime/helper/CLI变更。新增工具是独立版本化扩展，固定原上游源码保留。网络只允许固定HTTPS stock/get的GET请求、明确SH/SZ证券、最多3个证券、每次10秒与1MiB读取上限、一次尝试、禁重定向；未见TLS校验或凭证过滤降低。按同一响应的f43/f59缩放价格、f57/f107验证身份、f86秒级epoch还原供应商时间，不拼接不同采集时点。

发现并修复两个证据一致性问题：snapshot现比较从raw HTTP body+SHA复算得到的全部派生字段，以及请求endpoint/method/params，篡改cache_status、source_review或request均拒绝；HTTPError原先因继承URLError导致body遗失，现单独保存HTTP状态、有限正文/前缀SHA、请求和采集时间，不含headers或异常正文。独立mock HTTP403、正文`{"error":"denied"}`复核确认ok=false、无有效行，正文/status/hash/receipt完整保留且不回显合成异常详情。

独立检查当前安装的两个扩展文件与仓库源码字节完全一致，`verify_runtime`通过；已安装行情模块SHA-256为`73d7b386d45c50a234c646c3121bef5f367ef32fc4b2977e430b25ce69ebd157`。最终运行标准全套命令，**78/78通过**，CLI help退出0。本审计没有发网络请求或访问真实凭证。

只读核对采集代理已落盘证据目录 `data/integrations/ashare-timestamp-mcp-20260927T040053Z-bc039191`：quote、tools、request、summary、snapshot-validation的文件SHA sidecar均一致，catalog包含31工具及新时间戳工具；行情result.ok=true，原始HTTP正文SHA与独立解析一致，600519.SH价格1237、供应商f86为2026-09-24T08:11:44Z、f600为`-`、cache_status为unknown。这是已落盘真实MCP成功采集记录的复核，不是本审计另发请求核价，也不是供应商签名认证。

必须分开报告四件事：

1. 时间戳报价采集已经成功，不能再沿用旧版“实时工具完全没有时间”的结论。
2. f600缺有效币种标签；无registered CN instrument契约时snapshot实际拒绝。只在审计内存副本加明确CNY契约后归一化通过，不代表已注册真实证券资料或建立真实交易账户。
3. 此报价在采集时年龄为244156.325497秒，大于默认300秒；行情字段换算北京时间16:11:44也在当前连续竞价模拟时段之外。休市日期9/25–27不能将旧报价当新行情；既陈旧又非支持session，不能据此成交。官方UI字段语义不构成交易所逐笔成交时间保证。
4. 未执行交易，真实前向评测仍未验证；成功采集及78项本地测试均不能证明真实策略收益或模型能力。

在本次已检查范围中，没有尚未修复的P1/P2本地正确性发现；原历史失败与修复证据均保留。

## 当前币种契约与真实记录入库追加复核

只读检查 `data/validation/cn-real-snapshot-20260927T051503Z-c48917/account.db` 和 `result.json`，未再联网或重跑测试，SQLite使用`mode=ro`。核对result引用的原MCP capture文件SHA、instrument JSON文件SHA，以及公司披露/SSE主板/SSE人民币A股定义三份本地证据JSON和短摘录SHA均一致；证据URL与引用一致。这里只复核已保存摘录和推理链，不是本审计再次取得官网全文或供应商认证。

当前600519.SH的SH_MAIN_STOCK/CNY身份契约已补。source.available_at与collected_at均为本次核实时间2026-09-27T05:13:05.998893Z，晚于原MCP采集时间04:01:00.325497Z、早于账户注册事件，没有回填到历史报价日期。停牌、风险状态、上市序号和历史时点身份仍标unknown，不把当前身份契约当作未来或历史逐日可交易性证明。

独立重算数据库5个事件的hash链：init、instrument、quote、hold、snapshot，head与result一致；instrument payload/hash与证据文件一致，quote ID/evidence SHA、raw HTTP归一化、profile/source及原capture.result一致。入库价格1237、provider asof仍为9/24T08:11:44Z，original received_at仍为9/27T04:01:00.325497Z，未用入库时间冒充采集或行情时间。

独立核对账户为全新live CNY假钱账户、现金和初始现金均100000；orders、fills、positions、settlement lots、每日状态记录数量均为0。估值快照现金/净值100000，预留/费用均0；没有买卖、成交或投资收益。休市和陈旧报价拒绝符合预期保护，是成功拒绝而非代码缺陷。此补充解决之前“币种契约尚未注册”的具体证据缺口，真实前向评测仍未验证。

截至该只读收尾检查，已检查实现没有新增未修复的P1/P2问题；此前列明的模型限制与真实前向证据要求继续适用。
