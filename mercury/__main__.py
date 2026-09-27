"""JSON-only CLI suitable for agents: python -m mercury --help."""

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from .analytics import market_report, performance
from .audit import audit
from .common import canonical
from .engine import PaperTrader
from .market import FinancialDatasetsMCP, collect_quote, load_history, read_capture, save_capture


def demo():
    """Self-contained deterministic demonstration; never masquerades as live data."""
    with tempfile.TemporaryDirectory(prefix="mercury-demo-") as folder:
        t = PaperTrader.create(Path(folder) / "demo.db", start="2026-01-05T15:00:00Z")
        t.import_quote(ticker="DEMO", price="100", asof="2026-01-05T15:00:00Z", evidence={"synthetic": True})
        t.submit(ticker="DEMO", side="buy", qty=10, request_id="demo-buy")
        t.import_quote(ticker="DEMO", price="101", asof="2026-01-05T15:01:00Z", evidence={"synthetic": True})
        t.advance("2026-01-05T15:01:00Z")
        t.match()
        t.portfolio(record=True)
        o = t.submit(ticker="DEMO", side="sell", qty=2, request_id="demo-cancel", limit_price="120")
        t.cancel(o["id"])
        t.submit(ticker="DEMO", side="sell", qty=4, request_id="demo-sell")
        t.import_quote(ticker="DEMO", price="103", asof="2026-01-05T15:02:00Z", evidence={"synthetic": True})
        t.advance("2026-01-05T15:02:00Z")
        t.match()
        return {"synthetic": True, "report": performance(t), "audit": audit(t)}


def parser():
    p = argparse.ArgumentParser(description="Mercury 本地假钱账户；MCP 只读行情；JSON 输出")
    p.add_argument("--db", default="data/paper.db", help="单账户 SQLite 路径；不同模型/实验分库")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="创建账户；已有文件拒绝覆盖")
    init.add_argument("--cash", default="100000")
    init.add_argument("--fee-bps", default="5")
    init.add_argument("--slippage-bps", default="2")
    init.add_argument("--mode", choices=["demo", "replay", "live"], default="demo")
    init.add_argument("--market", choices=["US", "CN"], default="US", help="CN uses a separate CNY account; US preserves the existing USD model")
    init.add_argument("--calendar", help="sourced CN trading calendar JSON; required for CN")
    init.add_argument("--minimum-commission", default="5", help="CN minimum commission CNY per original order, cumulatively across partial fills")
    init.add_argument("--start", help="demo/replay 起始 ISO-8601 时间（含时区）")
    init.add_argument("--max-age", type=int, default=300, help="报价最长可用秒数，1..86400")
    init.add_argument("--risk-policy", help="explicit immutable CN hard risk policy JSON")
    init.add_argument("--execution-policy", help="parameterized local spread/latency/capacity JSON")
    for name in ("buy", "sell"):
        trade = sub.add_parser(name, help="提交待撮合订单；必须另行 match")
        trade.add_argument("ticker")
        trade.add_argument("qty", type=int)
        trade.add_argument("--request-id", required=True, help="幂等请求 ID；重试复用，不能改参数")
        trade.add_argument("--limit", dest="limit_price")
        trade.add_argument("--reason", default="")
    sub.add_parser("cancel").add_argument("order_id")
    sub.add_parser("register-instrument", help="CN instrument classification with source evidence").add_argument("path")
    sub.add_parser("register-session", help="CN per-date suspension, risk and price-limit reference").add_argument("path")
    sub.add_parser("hold", help="维持现金/持仓和挂单，记录决策及估值").add_argument("--reason", default="")
    sub.add_parser("advance").add_argument("at", help="仅 demo/replay 单调前进；之后运行 match")
    for name in ("match", "portfolio", "snapshot", "report", "audit", "orders", "fills", "events", "demo", "mcp-tools"):
        sub.add_parser(name)
    quote_live = sub.add_parser("collect-quote", help="直接通过官方托管 MCP 采集并记录报价")
    quote_live.add_argument("ticker")
    quote_live.add_argument("--currency", choices=["USD"], required=True, help="明确确认该标的以 USD 报价；provider 可能省略币种")
    quote = sub.add_parser("import-fixture", help="导入合成报价；live 账户禁止")
    quote.add_argument("path", help="fixture JSON 含 ticker,price,asof,evidence")
    history = sub.add_parser("collect-history", help="保存一份 MCP 历史响应；不承诺完整分页")
    history.add_argument("ticker")
    history.add_argument("--start", required=True)
    history.add_argument("--end", required=True)
    history.add_argument("--out", required=True)
    imp = sub.add_parser("load-history", help="将历史 capture 的 EOD 收盘价导入 demo/replay")
    imp.add_argument("ticker")
    imp.add_argument("path")
    analysis = sub.add_parser("analyze-history", help="本地计算截至 asof 可用历史数据指标")
    analysis.add_argument("ticker")
    analysis.add_argument("path")
    analysis.add_argument("--asof", required=True)
    sub.add_parser("ashare-tools", help="discover pinned local ashare MCP tools")
    sub.add_parser("ashare-quote", help="collect and store a timestamped CN quote; missing provider time is rejected").add_argument("ticker")
    ac = sub.add_parser("ashare-collect", help="capture one read-only ashare MCP response")
    ac.add_argument("ticker")
    ac.add_argument("--tool", choices=["get_realtime_quote", "mercury_get_realtime_quote", "get_daily_kline", "get_minute_kline", "get_stock_info"], required=True)
    ac.add_argument("--start")
    ac.add_argument("--end")
    ac.add_argument("--period", choices=["1", "5", "15", "30", "60"], default="5")
    ac.add_argument("--out", required=True)
    for name in ("ashare-load", "ashare-analyze"):
        cmd = sub.add_parser(name)
        cmd.add_argument("ticker")
        cmd.add_argument("path")
        if name == "ashare-analyze":
            cmd.add_argument("--asof", required=True)
    observation = sub.add_parser("observation", help="freeze a cutoff-filtered CN public observation")
    observation.add_argument("--universe", required=True, help="comma-separated qualified A-share symbols")
    observation.add_argument("--documents", help="bounded sourced document descriptors JSON")
    observation.add_argument("--lookback", type=int, default=60)
    observation.add_argument("--asof")
    observation.add_argument("--out", required=True)
    cycle = sub.add_parser("cycle", help="bounded JSON-agent CN cycles; no automatic background schedule")
    cycle.add_argument("--universe", required=True)
    cycle.add_argument("--agent", required=True, help="offline/OpenAI Responses/HTTPS JSON driver config")
    cycle.add_argument("--out", required=True, help="immutable private cycle artifact directory")
    cycle.add_argument("--rounds", type=int, default=1)
    cycle.add_argument("--lookback", type=int, default=60)
    cycle.add_argument("--documents")
    cycle.add_argument("--collect", action="store_true", help="fixed read-only live collector, one bounded call per round")
    cycle.add_argument("--captures", help="JSON list of new externally collected capture envelopes, one per round")
    for name in ("backtest", "compare"):
        cmd = sub.add_parser(name, help="bounded CN episode with identical observations/capital/rules")
        cmd.add_argument("--episode", required=True)
        cmd.add_argument("--agent" if name=="backtest" else "--agents", required=True)
        cmd.add_argument("--out", required=True)
    sub.add_parser("configure-risk", help="initialize immutable CN risk limits, not a model permission").add_argument("path")
    sub.add_parser("workflow-demo", help="complete synthetic CN episode; no live data or model API calls")
    sub.add_parser("workflow-example", help="write immutable synthetic episode and offline agent configs").add_argument("--out",required=True)
    return p


def run(args):
    command = args.command
    if command == "demo":
        return demo()
    def config(path):
        from .models import strict_json
        return strict_json(Path(path).read_text(encoding="utf-8-sig"))
    if command == "workflow-demo":
        from .examples import workflow_demo
        return workflow_demo()
    if command == "workflow-example":
        from .examples import write_workflow_example
        return write_workflow_example(args.out)
    if command in {"backtest", "compare"}:
        from .evaluation import backtest, compare_agents
        from .models import agent_from_config
        spec=config(args.episode)
        if command=="backtest":
            return backtest(spec,agent_from_config(config(args.agent)),output=args.out)
        agents={name:agent_from_config(value) for name,value in config(args.agents).items()}
        return compare_agents(spec,agents,output=args.out)
    if command == "init":
        calendar = json.loads(Path(args.calendar).read_text(encoding="utf-8-sig")) if args.calendar else None
        t = PaperTrader.create(args.db, cash=args.cash, fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                               mode=args.mode, start=args.start, max_age=args.max_age,
                               market=args.market, calendar=calendar, minimum_commission=args.minimum_commission,
                               risk_policy=config(args.risk_policy) if args.risk_policy else None,
                               execution_policy=config(args.execution_policy) if args.execution_policy else None)
        return t.portfolio()
    if command == "ashare-tools":
        from .ashare import AshareMCP
        return AshareMCP().tools()
    if command == "ashare-collect":
        from .ashare import AshareMCP, historical, snapshot, QUOTE_TOOLS
        from .cn import cn_symbol
        cn_symbol(args.ticker)
        if Path(args.out).exists():
            raise ValueError("capture already exists; choose a new path")
        arguments = {"symbols": [args.ticker]} if args.tool in QUOTE_TOOLS else {"symbol": args.ticker}
        if args.tool in {"get_daily_kline", "get_minute_kline"}:
            if not args.start or not args.end:
                raise ValueError("history collection requires explicit --start and --end")
            arguments.update(start_date=args.start, end_date=args.end, adjust="")
            if args.tool == "get_minute_kline":
                arguments["period"] = args.period
        capture = AshareMCP().call(args.tool, arguments)
        saved = save_capture(args.out, capture)
        try:
            if args.tool in QUOTE_TOOLS:
                snapshot(capture, args.ticker)
            elif args.tool in {"get_daily_kline", "get_minute_kline"}:
                historical(capture, args.ticker)
            else:
                return {"capture": saved, "usable_for_execution": False, "reason": "stock info is research metadata"}
        except (ValueError, KeyError, TypeError) as exc:
            return {"capture": saved, "usable_for_execution": False, "validation_error": str(exc)}
        return {"capture": saved, "validated": True, "usable_for_execution": args.tool in QUOTE_TOOLS}
    if command == "ashare-analyze":
        from .ashare import analyze_history
        return analyze_history(read_capture(args.path), args.ticker, asof=args.asof)
    if command == "mcp-tools":
        return FinancialDatasetsMCP().tools()
    if command == "collect-history":
        from datetime import date
        from .common import symbol
        if date.fromisoformat(args.start) > date.fromisoformat(args.end):
            raise ValueError("start must not exceed end")
        if Path(args.out).exists():
            raise ValueError("output already exists; choose a new capture path")
        capture = FinancialDatasetsMCP().call("get_stock_prices", {"ticker": symbol(args.ticker),
                "start_date": args.start, "end_date": args.end, "interval": "day", "interval_multiplier": 1})
        return save_capture(args.out, capture)
    if command == "analyze-history":
        return market_report(read_capture(args.path), args.ticker, asof=args.asof)
    t = PaperTrader(args.db)
    if command == "configure-risk":
        return t.configure_risk(config(args.path))
    if command == "observation":
        from .observation import build_observation
        from .workflow import artifact
        observation=build_observation(t,args.universe.split(","),documents=config(args.documents) if args.documents else (),lookback=args.lookback,as_of=args.asof)
        artifact(args.out,observation)
        return {"observation_id":observation["observation_id"],"output":args.out,"as_of":observation["as_of"]}
    if command == "cycle":
        from .models import agent_from_config
        from .workflow import run_cycles
        return run_cycles(t,agent_from_config(config(args.agent)),args.universe.split(","),output=args.out,
                          rounds=args.rounds,lookback=args.lookback,documents=config(args.documents) if args.documents else (),
                          collect=args.collect,captures=config(args.captures) if args.captures else ())
    if command == "ashare-quote":
        from .ashare import collect_quote
        return collect_quote(t, args.ticker)
    if command in {"register-instrument", "register-session"}:
        data = json.loads(Path(args.path).read_text(encoding="utf-8-sig"))
        return t.register_instrument(data) if command == "register-instrument" else t.register_session(data)
    if command == "ashare-load":
        from .ashare import load_history
        return load_history(t, read_capture(args.path), args.ticker)
    if command in {"buy", "sell"}:
        return t.submit(ticker=args.ticker, side=command, qty=args.qty, request_id=args.request_id,
                        limit_price=args.limit_price, reason=args.reason)
    if command == "cancel":
        return t.cancel(args.order_id)
    if command == "hold":
        return t.hold(args.reason)
    if command == "advance":
        return t.advance(args.at)
    if command == "match":
        return t.match()
    if command in {"portfolio", "snapshot"}:
        return t.portfolio(record=command == "snapshot")
    if command == "report":
        return performance(t)
    if command == "audit":
        return audit(t)
    if command in {"orders", "fills", "events"}:
        return t.history(command)
    if command == "collect-quote":
        return collect_quote(t, args.ticker, currency=args.currency)
    if command == "import-fixture":
        data = json.loads(Path(args.path).read_text(encoding="utf-8-sig"))
        if not {"ticker", "price", "asof", "evidence"}.issubset(data) or set(data) - {"ticker", "price", "asof", "evidence", "currency"}:
            raise ValueError("fixture requires ticker,price,asof,evidence and optional explicit currency")
        return t.import_quote(**data, kind="fixture", source="fixture")
    if command == "load-history":
        return load_history(t, read_capture(args.path), args.ticker)
    raise ValueError("unsupported command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(canonical(result))
        return 1 if args.command == "audit" and not result["ok"] else 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, ArithmeticError) as exc:
        # External response bodies and secrets are never included in diagnostics.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(canonical({"error": message, "command": args.command}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
