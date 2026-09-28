# Mercury

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

Local paper trading for AI agents: **ashare-mcp supplies A-share research data, Financial Datasets MCP remains the global-data foundation, and Python manages separate CNY/USD simulated accounts.** No brokerage connection or real-money orders. The core requires only the Python 3.11+ standard library.

> Status: the A-share research/strategy/risk/execution/review loop is implemented. See the [workflow guide](docs/agent-workflow.md) and [acceptance evidence](docs/repository-acceptance.md). Real timestamped MCP capture is verified; real forward fills and live model API execution remain unverified. FDM authentication works; quote access was blocked by credits/balance at the last check.

```text
Collector subagent -> ashare-mcp (CN) / FDM (global) -> Raw response / time / SHA-256
                                                        |
                                             Local validation and analysis
                                                        |
Frozen Observation -> Research / Strategy JSON -> TradeIntent -> Hard risk limits
                                                        |
                                  SQLite paper fills -> Review -> Next observation
                                                        |
                                           Independent audit + primary review
```

## Complete A-share agent cycle

```powershell
python -m mercury workflow-demo
```

This synthetic example runs two offline agents with identical CNY capital, observations, fees and rules. Each completes a T+1 buy/sell episode across the September 25–27 closure. It uses no model API or real quotes. The mandatory cycle is documented in [AGENTS.md](AGENTS.md): collect, freeze, research, compile intent, enforce risk, execute, review, independently audit.

`cycle` runs a bounded number of rounds with an offline agent, the OpenAI Responses API, or an explicit HTTPS JSON service. Credentials come only from environment variables. The model receives frozen data and returns a strict strategy DSL; it gets no local execution tools. `backtest` and `compare` replay chronological episodes using separate accounts and shared observations. Setup and JSON formats are in the [workflow guide](docs/agent-workflow.md).

CN accounts can enforce immutable position, order amount, daily turnover and daily loss limits through `--risk-policy`. Such accounts reject direct `buy`/`sell` calls; execution goes through validated TradeIntent batches. Optional `--execution-policy` adds spread, latency and partial fills, with shared quote capacity and cumulative per-order fees. These are configurable simulation assumptions, not measured order-book liquidity. Model API access and profitable forward performance have not been established by the synthetic demo.

## A-share experiments (primary market)

A-share experiments use **ashare-mcp → a separate CNY account**. Financial Datasets remains the global-data foundation and an independent USD comparison. Markets and currencies are not mixed in the primary experiment.

```powershell
python -m mercury ashare-tools
python -m mercury --db data/cn-demo.db init --market CN --mode demo --calendar config/cn-calendar-2026.json --start 2026-09-24T10:00:00+08:00
python -m mercury --db data/cn-demo.db register-instrument examples/cn-instrument.json
python -m mercury --db data/cn-demo.db register-session examples/cn-session.json
python -m mercury --db data/cn-demo.db import-fixture examples/cn-quote-1000.json
python -m mercury --db data/cn-demo.db buy 600519.SH 100 --limit 11 --request-id cn-demo-buy-001
python -m mercury --db data/cn-demo.db import-fixture examples/cn-quote-1001.json
python -m mercury --db data/cn-demo.db advance 2026-09-24T10:01:00+08:00
python -m mercury --db data/cn-demo.db match
python -m mercury --db data/cn-demo.db report
python -m mercury --db data/cn-demo.db audit
```

These examples use **synthetic quotes and security metadata**, not the real price of the named instrument. CN requires sourced instrument classification and daily suspension/risk/listing/price-limit metadata, with symbol/date bindings and availability timestamps. The bundled calendar covers 2026; the current rules apply only from **2026-07-06 through 2026-12-31**. September 25–27 are the Mid-Autumn closure; stock bought on September 24 becomes sellable on September 28.

CN supports T+1 lots, verified ETF-specific T+0 profiles, sellable/reserved quantities, per-board lot/tick/order limits, dated price bands, and commission/transfer/stamp-tax components. It accepts limit orders during continuous sessions, excludes auctions and unprotected market orders, and conservatively refuses buying at limit-up or selling at limit-down without liquidity evidence. DAY orders expire with reservations released; account views/reports may append expiry events. `audit` remains read-only.

`mercury-ashare` has been registered as a local Codex stdio server; a new chat/reload may be needed. The core stays standard-library only; the separately reviewed MCP environment and local paths are documented in the [A-share setup guide](docs/ashare-setup.md) (Simplified Chinese). Source manifests and launcher hashes are checked before launch.

Real evidence: 30 upstream tools plus one Mercury extension were discovered. A 2026-09-24 daily bar and a real timestamped snapshot were collected. The snapshot reports 2026-09-24 16:11:44 (UTC+8), so it is stale during the September 25–27 closure; its missing currency requires a registered CNY instrument contract. `ashare-quote` now uses `mercury_get_realtime_quote`, with raw-response price/time/identity checks and separate extension hashes. Original quote/minute/info failures remain preserved. Daily bars remain research/valuation only; no real forward fill has been verified. See [field provenance](docs/ashare-timestamp-source.md) and the [independent audit](docs/ashare-audit-review.md).

## Quick start

Run from the repository root. No `pip install` is required:

```powershell
python -m mercury demo
python -m unittest discover -s tests -v
python -m mercury --help
```

`demo` uses temporary synthetic quotes to demonstrate buying, cancellation, selling, retained positions, reporting, and auditing. It removes its own temporary account on exit. This does not test a real market-data connection.

## Local accounts

```powershell
python -m mercury --db data/demo.db init --mode demo --start 2026-01-05T15:00:00Z
python -m mercury --db data/demo.db import-fixture examples/quote-100.json
python -m mercury --db data/demo.db buy DEMO 10 --request-id decision-001 --reason "Demo buy"
python -m mercury --db data/demo.db import-fixture examples/quote-101.json
python -m mercury --db data/demo.db advance 2026-01-05T15:01:00Z
python -m mercury --db data/demo.db match
python -m mercury --db data/demo.db hold --reason "Maintain position"
python -m mercury --db data/demo.db portfolio
python -m mercury --db data/demo.db report
python -m mercury --db data/demo.db audit
```

- `buy` / `sell` submit orders; `match` explicitly processes them. Fill prices come from recorded quotes, slippage, and limit conditions. Trading commands do not accept a fill-price parameter.
- `--limit 99` sets the maximum buy price or minimum sell price. It does not force a fill at that price. An order stays open if the price after slippage violates its limit.
- `cancel <order_id>` cancels an unfilled order and releases its reservation. Filled orders cannot be cancelled.
- `hold` records a decision to keep the position and a valuation snapshot; it does not cancel existing orders. Cash, orders, and positions persist across restarts.
- `--request-id` is required. Reuse it when retrying the same decision; reusing it with different parameters fails.
- Each database contains one account/experiment. Defaults: 100,000 USD, a 5 bps fee, and 2 bps slippage (1 bp = 0.01%). Set these at `init`; existing account settings are not changed afterward.
- Whole shares, long positions and no leverage; partial fills are configurable for new accounts. Buy orders reserve cash; sell orders reserve remaining shares. SQLite serializes write transactions. A market order is rejected if a price jump makes it exceed available cash.
- demo/replay orders wait for a quote later than submission; live uses the latest eligible MCP quote at the current time. Quotes expire after 300 seconds by default. Stale quotes cannot fill orders; equity is `null` when a held position lacks a fresh valuation.
- Decimal accounting uses eight decimal places. Buy fees are included in the position's cost basis; sales use average-cost allocation. Net returns include fees on both sides.
- No background loop or scheduled task runs automatically. The agent invokes each collection, matching, and reporting step through the CLI/API.

## Real market data → simulated trading

Set `FINANCIAL_DATASETS_API_KEY` in your local environment. On Windows, open **Edit environment variables for your account → User variables → New**. Use that exact variable name and paste your key as its value; do not change `PATH`. Restart your terminal/Codex to inherit it. The [setup guide](docs/market-setup.md) provides more detail (Simplified Chinese).

The code does not read key files, accept keys as command-line arguments, or write a `.env` file. Never put a key in source code, captures, chat, or Git.

```powershell
python -m mercury mcp-tools
python -m mercury --db data/forward-agent-a.db init --mode live
python -m mercury --db data/forward-agent-a.db collect-quote AAPL --currency USD
python -m mercury --db data/forward-agent-a.db buy AAPL 1 --request-id forward-001 --reason "Your strategy rationale"
python -m mercury --db data/forward-agent-a.db match
python -m mercury --db data/forward-agent-a.db snapshot
python -m mercury --db data/forward-agent-a.db audit
```

`live` means current-time/MCP-data mode; **all funds and executions remain simulated**. Collection and trading are separate; the MCP client has no order-placement capability. `--currency USD` explicitly confirms the instrument's quote currency: the provider's snapshot schema may omit currency, so it must not be silently guessed. A conflicting currency is rejected.

The client uses the [official hosted MCP](https://docs.financialdatasets.ai/mcp-server) `/api` endpoint. It initializes, discovers tools, then calls `get_stock_price` / `get_stock_prices`. JSON and SSE responses are supported. Unrecognized response shapes, missing timestamps, or invalid provenance are rejected for execution. Real account permissions, quotas, and response formats still need connection testing; mocks do not establish that they work.

## Historical data and local analysis

```powershell
python -m mercury collect-history AAPL --start 2025-01-01 --end 2025-01-31 --out data/captures/aapl-202501.json
python -m mercury analyze-history AAPL data/captures/aapl-202501.json --asof 2025-02-01T00:00:00Z
python -m mercury --db data/replay-a.db init --mode replay --start 2025-01-01T00:00:00Z --max-age 86400
python -m mercury --db data/replay-a.db load-history AAPL data/captures/aapl-202501.json
```

Historical imports are atomic: an invalid row rolls back the entire batch. After import, use `advance` at the actual data dates, observe and decide, then advance to a later quote and run `match`. Daily bars become available at the next UTC midnight; the day's closing price is not visible at its opening. Local analysis computes price return, drawdown, a 20-period moving average, and sample return volatility, using only rows available by `asof`.

`collect-history` saves one tool response; **it is not a full-market downloader**. Pagination is marked complete only when the response explicitly contains `next_page_url: null`; missing pagination metadata means unknown. If there is another page, collect it separately or shorten the requested window. A single response does not establish that a 5–10 year dataset is complete. See the [evaluation conventions](docs/evaluation.md) (Simplified Chinese).

## Auditing and limitations

`audit` checks event hashes, quote provenance bindings, order states, fees, slippage, and limits without modifying the ledger. It independently reconstructs cash, positions, cost basis, and recorded valuations. A nonzero exit code indicates a failed check or an error. This is a local consistency check; separate subagent review follows [AGENTS.md](AGENTS.md) (Simplified Chinese) and does not replace Codex Auto Review.

SHA-256 establishes local content consistency, not a provider signature. Private Python methods and a local database do not isolate a malicious agent with process or file access. Strict evaluation requires a separate runner, read-only observations, and externally anchored evidence that the agent cannot rewrite.

Not implemented: US exchange calendars/closed-market restrictions and auction mechanics, measured order-book depth/queue simulation, market impact, splits/dividends, delisting and survivorship corrections, an anonymized benchmark runner, a large historical dataset, or long-term forward evaluation. This is testable simulation infrastructure, not a completed benchmark that establishes investment or model performance.

## Project layout

| Path | Purpose |
| --- | --- |
| `mercury/` | Trading engine, market adapters, analysis, audit, and JSON CLI |
| `tests/` | Local regression tests |
| `examples/` | Explicitly marked synthetic quotes |
| `docs/` | Setup, evaluation conventions, and independent audit report (Simplified Chinese) |
| `scripts/` | Windows environment-based connection check |
| `data/` | Private experiment databases and captures; ignored by Git |
| `.github/workflows/checks.yml` | Local tests and demo on Python 3.11 and 3.13 |

Creating an account or capture refuses to overwrite an existing file. The project does not commit or push automatically.

## Development

Before proposing changes, run the quick-start checks and read [AGENTS.md](AGENTS.md). Use a feature branch and a pull request; preserve existing data and changes. Substantive changes to execution, fees, ingestion, or evaluation require review by an independent subagent. Keep all three README versions synchronized. A local test pass is not proof of remote CI or real MCP availability.

## License

[Apache License 2.0](LICENSE).
