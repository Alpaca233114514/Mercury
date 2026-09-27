"""Controlled CN replay episodes and shared-observation comparison, no new matcher."""
import pathlib

from .analytics import performance, sharpe_for_schedule
from .common import canonical, digest, number, timestamp
from .engine import PaperTrader
from .models import agent_from_config
from .workflow import artifact, read_artifact, run_cycle, run_lock


def validate_episode(spec):
    fields = {"version", "universe", "cash", "calendar", "instruments", "sessions", "quotes", "history_captures",
              "events", "documents", "lookback", "fee_bps", "slippage_bps", "minimum_commission", "risk_policy",
              "execution_policy", "sampling", "benchmark", "synthetic"}
    if not isinstance(spec, dict) or spec.get("version") != "cn-episode-v1" or set(spec)-fields:
        raise ValueError("unknown or invalid CN episode configuration")
    from .cn import cn_symbol, validate_calendar
    validate_calendar(spec["calendar"])
    universe = spec["universe"]
    if not isinstance(universe, list) or not universe or universe != sorted(set(universe)):
        raise ValueError("episode universe must be sorted, nonempty and unique")
    for ticker in universe:
        cn_symbol(ticker)
    number(spec["cash"], positive=True)
    events = spec["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 1000:
        raise ValueError("episode requires 1..1000 bounded events")
    times = []
    for event in events:
        if not isinstance(event, dict) or set(event) != {"time", "decide"} or type(event["decide"]) is not bool:
            raise ValueError("episode event requires only time and boolean decide")
        times.append(timestamp(event["time"]))
    if times != sorted(set(times)):
        raise ValueError("episode event clocks must be strictly increasing")
    if not isinstance(spec.get("risk_policy"), dict):
        raise ValueError("episode requires explicit hard risk policy")
    if spec.get("quotes") and spec.get("synthetic") is not True:
        raise ValueError("fixture quotes require explicit synthetic=true; real history uses bound captures")
    for quote in spec.get("quotes", []):
        if (not isinstance(quote, dict) or set(quote) != {"ticker", "price", "asof", "evidence"} or
                quote["ticker"] not in universe or quote["evidence"].get("synthetic") is not True):
            raise ValueError("synthetic quote requires qualified universe ticker and explicit fixture evidence")
        number(quote["price"], positive=True)
        timestamp(quote["asof"])
    return spec


def _account(path, spec):
    if path.exists():
        trader = PaperTrader(path)
        from .audit import audit
        if not audit(trader)["ok"]:
            raise ValueError("existing episode ledger failed audit; not repaired automatically")
    else:
        trader = PaperTrader.create(path, cash=spec["cash"], market="CN", mode="replay",
            start=spec["events"][0]["time"], calendar=spec["calendar"], fee_bps=spec.get("fee_bps", "3"),
            slippage_bps=spec.get("slippage_bps", "2"), minimum_commission=spec.get("minimum_commission", "5"),
            risk_policy=spec["risk_policy"], execution_policy=spec.get("execution_policy"))
    receipt_path = pathlib.Path(str(path)+".seed.json")
    binding = {"episode_sha256":digest(spec), "account":digest(str(path.resolve()))}
    from .cn import create_config
    from .risk import load,validate_policy,validate_execution
    expected_market=create_config(spec["calendar"],spec.get("fee_bps","3"),spec.get("minimum_commission","5"))
    with trader._read() as db:
        account=trader._account(db)
        initialized=db.execute("SELECT time,payload FROM events WHERE kind='init' ORDER BY seq LIMIT 1").fetchone()
        market=trader._policy(db)
        if (account["currency"]!="CNY" or account["mode"]!="replay" or market is None or
                number(account["initial_cash"])!=number(spec["cash"]) or
                number(account["fee_bps"])!=number(spec.get("fee_bps","3")) or
                number(account["slippage_bps"])!=number(spec.get("slippage_bps","2")) or
                account["max_age"]!=300 or initialized is None or
                timestamp(initialized["time"])!=timestamp(spec["events"][0]["time"]) or
                digest(market.config)!=digest(expected_market) or
                load(db,"risk_config")!=validate_policy(spec["risk_policy"]) or
                load(db,"execution_config")!=validate_execution(spec.get("execution_policy"))):
            raise ValueError("existing episode account initialization/policies differ from frozen comparison contract")
    if receipt_path.exists():
        if read_artifact(receipt_path) != binding:
            raise ValueError("account seed receipt differs from episode")
        return trader
    if trader.history("orders") or trader.history("fills"):
        raise ValueError("unfinished seed unexpectedly contains trading activity")
    from .cn import sourced
    for table, items in (("instruments",spec["instruments"]),("instrument_days",spec["sessions"])):
        for item in items:
            expected=dict(item,source=sourced(item,day=item.get("day")))
            with trader._read() as db:
                if table == "instruments":
                    existing=db.execute("SELECT payload,hash FROM instruments WHERE symbol=?",(item["symbol"],)).fetchone()
                else:
                    existing=db.execute("SELECT payload,hash FROM instrument_days WHERE symbol=? AND day=?",(item["symbol"],item["day"])).fetchone()
                    expected["reference_price"]=str(number(item["reference_price"]))
                    cn=trader._policy(db);up,down=cn.bands(cn.instrument(db,item["symbol"]),expected)
                    expected.update(limit_up=None if up is None else str(up),limit_down=None if down is None else str(down))
            if existing is not None:
                import json
                if digest(json.loads(existing[0])) != existing[1] or digest(expected) != existing[1]:
                    raise ValueError("existing seed metadata is not equivalent; no overwrite permitted")
            elif table == "instruments":
                trader.register_instrument(item)
            else:
                trader.register_session(item)
    for quote in spec.get("quotes", []):
        # Explicit synthetic receipt time ensures identical IDs across accounts.
        trader._store_quote(ticker=quote["ticker"], price=quote["price"], asof=quote["asof"],
            evidence=quote["evidence"], source="episode-fixture", kind="fixture", currency="CNY", received_at=quote["asof"])
    for item in spec.get("history_captures", []):
        from .ashare import load_history
        if set(item) != {"ticker", "capture"} or item["ticker"] not in spec["universe"]:
            raise ValueError("history capture binding outside episode universe")
        load_history(trader, item["capture"], item["ticker"])
    artifact(receipt_path,binding)
    return trader


def ledger_anchor(trader):
    with trader._read() as db:
        return db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0]


def aligned_benchmark(spec, points, universe):
    """Explicit aligned price series; index is never registered as a tradable stock."""
    if spec is None:
        return {"return": None, "null_reason": "no sourced aligned benchmark supplied", "coverage": 0}
    fields = {"kind", "symbol", "currency", "universe", "reference", "source_hash", "series", "includes_fees", "includes_dividends"}
    if (not isinstance(spec, dict) or set(spec) != fields or spec["kind"] not in {"index-price-return", "buy-hold-price-return"} or
            spec["currency"] != "CNY" or spec["universe"] != universe or not isinstance(spec["reference"], str) or not spec["reference"].strip() or
            not isinstance(spec["source_hash"], str) or len(spec["source_hash"]) != 64 or
            type(spec["includes_fees"]) is not bool or type(spec["includes_dividends"]) is not bool):
        raise ValueError("benchmark requires sourced same-universe CNY price-series contract")
    from .observation import sha
    sha(spec["source_hash"])
    series = {}
    for row in spec["series"]:
        if not isinstance(row, dict) or set(row) != {"asof", "available_at", "value", "source_hash"}:
            raise ValueError("benchmark row requires value/time/availability/hash")
        at = timestamp(row["asof"])
        if at in series or not isinstance(row["source_hash"], str) or len(row["source_hash"]) != 64:
            raise ValueError("duplicate or unbound benchmark row")
        sha(row["source_hash"])
        series[at] = (number(row["value"], positive=True), timestamp(row["available_at"]))
    aligned = [series.get(timestamp(p["time"])) for p in points]
    coverage = sum(row is not None and row[1] <= timestamp(p["time"]) for row,p in zip(aligned,points))
    complete = coverage == len(points) and len(points) >= 2
    return {"return": str(aligned[-1][0]/aligned[0][0]-1) if complete else None,
            "null_reason": None if complete else "benchmark coverage/availability does not match every episode valuation",
            "coverage": coverage, "expected_points": len(points), "kind": spec["kind"], "symbol": spec["symbol"],
            "source_hash": spec["source_hash"], "reference": spec["reference"], "asof": points[-1]["time"] if points else None,
            "includes_fees": spec["includes_fees"], "includes_dividends": spec["includes_dividends"],
            "series_sha256": digest(spec["series"]), "warning": "supplied price-series baseline, not a separately traded benchmark portfolio"}


def compare_agents(spec, agents, *, output):
    """Same frozen market JSON, initial CNY capital, clock, fees/rules across agents."""
    spec = validate_episode(spec)
    if not isinstance(agents, dict) or not 1 <= len(agents) <= 20 or any(not isinstance(name,str) or not name for name in agents):
        raise ValueError("comparison requires one to twenty uniquely named agents")
    root = pathlib.Path(output)
    from .observation import build_observation
    with run_lock(root):
        artifact(root/"comparison-manifest.json", {"version": "cn-comparison-v1", "episode": spec,
                 "agents": {name:agent.identity for name,agent in agents.items()}, "episode_sha256": digest(spec)})
        data = _account(root/"market-observer.db", spec)
        traders = {name:_account(root/("agent-"+digest(name)[:16]+".db"),spec) for name in agents}
        if (root/"comparison-report.json").exists():
            prior=read_artifact(root/"comparison-report.json")
            if prior.get("episode_sha256") != digest(spec) or set(prior.get("models",{})) != set(agents):
                raise ValueError("comparison report manifest binding mismatch")
            for name,trader in traders.items():
                if prior["models"][name].get("ledger_anchor") != ledger_anchor(trader):
                    raise ValueError("completed comparison ledger changed")
            return prior
        results = {name:[] for name in agents}
        feedback = {name:None for name in agents}
        points = {name:[] for name in agents}
        for index,event in enumerate(spec["events"]):
            at = timestamp(event["time"])
            event_path = root/f"event-{index:04d}.json"
            if event_path.exists():
                prior=read_artifact(event_path)
                obs=read_artifact(root/f"observation-{index:04d}.json")
                if prior.get("time")!=at or prior.get("episode_sha256")!=digest(spec) or prior.get("observation_id")!=obs["observation_id"]:
                    raise ValueError("cached episode event binding mismatch")
                for name in agents:
                    points[name].append(prior["valuations"][name])
                    if name in prior["cycles"]:
                        results[name].append(prior["cycles"][name]);feedback[name]=prior["cycles"][name]
                continue
            for trader in [data,*traders.values()]:
                if timestamp(trader.portfolio()["time"]) < at:
                    trader.advance(at)
                elif timestamp(trader.portfolio()["time"]) > at:
                    raise ValueError("episode recovery clock advanced beyond unfinished event")
                trader.match()
            obs_path=root/f"observation-{index:04d}.json"
            observation=read_artifact(obs_path) if obs_path.exists() else artifact(obs_path,build_observation(data,spec["universe"],documents=spec.get("documents",()),lookback=spec.get("lookback",60)))
            cycles,valuations={},{}
            for name,agent in agents.items():
                if event["decide"]:
                    folder=root/("cycles-"+digest(name)[:16])/f"cycle-{index:04d}"
                    try:
                        cycle=run_cycle(traders[name],agent,observation,output=folder,feedback=feedback[name])
                    except ValueError:
                        cycle={"status":"refused","observation_id":observation["observation_id"],"reason":"cycle refused; see fixed phase diagnostic"}
                    cycles[name]=cycle;results[name].append(cycle);feedback[name]=cycle
                value=traders[name].portfolio(record=True)
                valuations[name] = {"time":at,"equity":value["equity"]};points[name].append(valuations[name])
            artifact(event_path,{"time":at,"episode_sha256":digest(spec),"observation_id":observation["observation_id"],"cycles":cycles,"valuations":valuations})
        reports={}
        for name,trader in traders.items():
            reports[name]={"agent":agents[name].identity,"report":performance(trader),
                           "sampling":sharpe_for_schedule(points[name],spec.get("sampling")),
                           "benchmark":aligned_benchmark(spec.get("benchmark"),points[name],spec["universe"]),
                           "cycles":len(results[name]),"refused_cycles":sum(c.get("status")=="refused" for c in results[name]),
                           "ledger_anchor":ledger_anchor(trader)}
        result={"version":"cn-comparison-report-v1","episode_sha256":digest(spec),"initial_cash":str(number(spec["cash"])),
                "currency":"CNY","universe":spec["universe"],"events":len(spec["events"]),
                "first_time":timestamp(spec["events"][0]["time"]),"last_time":timestamp(spec["events"][-1]["time"]),
                "models":reports,"shared_observations":True,"synthetic":spec.get("synthetic") is True,
                "limitations":["shared data protocol does not isolate a trusted host administrator",
                               "parameterized local execution, not exchange fills or proven investment skill",
                               "model pretraining memory, survivorship and corporate actions not removed"]}
        artifact(root/"comparison-report.json",result)
        return result


def backtest(spec, agent, *, output):
    return compare_agents(spec,{"strategy":agent},output=output)
