# 东方财富实时报价时间字段来源审查

审查日期：2026-09-27。仅只读取得官方 HTML/JavaScript，未执行脚本，未调用行情 API。本报告证明官方页面字段使用方式；不代表已成功连接真实行情，也不代表交易所逐笔成交时间保证。

## 官方来源与发现

- [贵州茅台官方行情页面](https://quote.eastmoney.com/sh600519.html) 的 HTML 加载 `/newstatic/build/vendor.js` 和 `/newstatic/build/a.js`；内置 `quotedata` 标明 `market:1`、`quotecode:1.600519`。
- [平安银行官方行情页面](https://quote.eastmoney.com/sz000001.html) 内置 `market:0`、`quotecode:0.000001`。
- [官方 vendor.js](https://quote.eastmoney.com/newstatic/build/vendor.js) 的证券字段枚举把“交易时间”设为95，字段95使用 `f86`；同一响应的 `getQuoteInfo` 使用 `transTradeTime(e.data.f86)`。时间转换按 `new Date(1e3*e)` 进行，因此字段单位为 Unix 秒。JS 使用浏览器本地时间格式化，不应把显示字符串解释为服务器时区；本地解析直接按 UTC epoch 转换。
- [官方 a.js](https://quote.eastmoney.com/newstatic/build/a.js) 在标题 `quote_title_time` 渲染“交易时间带星期”，其字段97同样引用 `f86`。所以这是数据响应内的供应商交易时间字段，不能替换为本地抓取时间。公开脚本并未保证它一定是最后一笔成交时间或交易所认证时间，不能扩展该结论。
- `vendor.js` 最新价字段4使用 `f43` 与 `f59`，格式器除以 `10**precision`。官方 `stock/get` 调用使用 `fltt=1,invt=2`；该模式须读取原始整数价格并按 `f59` 缩放，不能统一除100。
- 同一脚本的市场字段1为 `f107`，代码字段2为 `f57`；统一行情链接组合 `f107.code`。因此响应应同时验证 `f57` 与 `f107`，不得仅凭请求代码绑定价格。
- `f600` 被标为交易币种；桥保留其原值。明确的 `CNY` 或“人民币”标签可规范化为 CNY；其他明确标签拒绝，不能忽略显式非 CNY 值。缺失、空白或占位符不补币种，本地注册的证券合约负责账户币种。

对取得响应解码后的字符串重新 UTF-8 编码所得 SHA-256（不是 HTTP 压缩线缆字节哈希）：

| 来源 | SHA-256 |
| --- | --- |
| vendor.js | `504cabef35a0f41ceb8ab4905c911ad317f47e36eabf82e3775bed30acc6d683` |
| a.js | `6adc733d354495708ef160df20ca2c59a3051ec0695d90b8dde85121be3f17b9` |

未保存完整脚本。哈希对应此次只读观察；网页和脚本可以变更，重新验证不能只看 URL。

## AkShare 交叉检查和上游缺口

[AkShare 官方报价源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/stock_ask_bid_em.py) 与已隔离安装版本均向 stock/get 请求 `f86`，但构造输出表时丢弃它。其 `fltt=2` 返回值与本报告采用的官方 UI `fltt=1` 不同，不混用两种价格缩放。 [官方文档](https://akshare.akfamily.xyz/data/stock/stock.html) 的报价输出也没有行情时间。

[AkShare 官方指数源码](https://github.com/akfamily/akshare/blob/main/akshare/index/index_global_em.py) 将 clist 的 `f124` 标记为最新行情时间并按 epoch 秒转换。这是不同接口的支持证据，不能直接把 stock/get 的 `f124` 解释为同一字段。优先采用已被股票页面直接渲染的 `f86`。

固定 CharmYue 上游实时主路径调用 `stock_zh_a_spot_em`，其最终表无时间；盘口路径也丢弃 `f86`。因此不用本地抓取时间“修补”原工具，也不修改固定源码与既有证据。

## 最小桥设计

新增独立、版本化只读工具 `mercury_get_realtime_quote`，原30个工具保留。对明确 `.SH`、`.SZ` 请求，每个证券单独读取同一 `https://push2.eastmoney.com/api/qt/stock/get` 响应。

参数：`fields=f43,f57,f59,f86,f107,f600`、`fltt=1`、`invt=2`、`secid=1.CODE`（SH）或 `0.CODE`（SZ）。只支持这两种显式市场，不从代码前缀、证券名称猜市场或证券类型。

验证 `rc=0`、响应代码与市场一致、整数价格与精度、正整数 epoch 秒且不晚于采集时间。保留完整成功响应 JSON、原始响应正文、请求参数、各证券独立行情时间、采集时间和原始字节 SHA-256。未知缓存状态明确写 unknown，不将缓存创建或抓取时间当报价时间。

最多3个证券；10秒超时；每个响应最多1MiB；不重试；默认 HTTPS TLS 验证；不跟随重定向。失败返回固定错误类别，不包含环境、HTTP头、代理地址或异常正文。已收到但验证失败的响应正文与 SHA 保留于 errors 中；非 UTF-8 正文以 base64 保存；超长响应只保留最多1MiB前缀并明确截断，其哈希只覆盖保留前缀。连接失败未收到正文时没有原始正文证据。混合批次出现失败则 `ok:false`，不能把部分行当成功批次成交。

尚待真实链路验证：必要响应字段实际可用性、供应商时间与页面一致性、闭市快照陈旧处理、代理连接及主机网络。2026-09-25至27休市；24至25日请求只返回24日日线符合日历，不能以休市解释 ProxyError。
