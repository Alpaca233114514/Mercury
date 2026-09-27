"""Bounded, journaled CN cycles. Protocol boundary, not host-admin isolation."""
import contextlib
import json
import os
import pathlib
import uuid

from .common import age_seconds, canonical, digest, money, number, timestamp
from .audit import audit


def _publish(path, raw):
    """Publish a complete immutable file; identical recovery is allowed."""
    path = pathlib.Path(path)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("immutable workflow artifact differs; choose a new run identity")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (".publish-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("concurrent immutable artifact conflict") from None
    finally:
        temporary.unlink(missing_ok=True)


def artifact(path, value):
    raw = canonical(value).encode("utf-8")
    import hashlib
    _publish(pathlib.Path(str(path) + ".sha256"), hashlib.sha256(raw).hexdigest().encode("ascii"))
    _publish(path, raw)
    return value


def read_artifact(path):
    import hashlib
    path = pathlib.Path(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pathlib.Path(str(path)+".sha256").read_text(encoding="ascii"):
        raise ValueError("workflow artifact hash mismatch")
    return json.loads(raw)


def phase(folder, name, operation):
    try:
        return operation()
    except Exception as exc:
        # Fixed diagnostic only: provider/model text never reaches persisted failures.
        artifact(pathlib.Path(folder)/("failure-"+name+"-"+uuid.uuid4().hex+".json"),
                 {"phase": name, "status": "failed", "error_type": type(exc).__name__,
                  "detail": "phase refused or unavailable; no external text retained"})
        raise ValueError("workflow phase failed: " + name) from None


def economic_identity(account):
    return digest({k:v for k,v in account.items() if k not in {"time", "snapshot_at", "decision_binding"}})


def checked_audit(trader):
    """Only successful local consistency evidence may enter a cycle receipt."""
    result = audit(trader)
    if result.get("ok") is not True:
        raise ValueError("local ledger consistency check refused")
    return {key:result[key] for key in
            ("ok", "fills_checked", "events_checked", "reconstructed_cash", "event_head", "limitations")}


def public_feedback(feedback):
    if feedback is None:
        return {}
    if not isinstance(feedback,dict):
        raise ValueError("previous review must be a local receipt object")
    if feedback.get("version") == "cn-cycle-review-v1":
        return {"cycle_id":feedback["cycle_id"],"risk_status":feedback["risk_status"],
                "risk_reasons":feedback["risk"]["reasons"],"portfolio":feedback["portfolio"],
                "execution":feedback["execution"]}
    return {"previous_cycle_status":"refused","reason":"previous cycle did not pass validation"}


@contextlib.contextmanager
def run_lock(folder):
    """OS file lock releases on process exit, including interrupted recovery."""
    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".runner.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("workflow is already running for this artifact directory") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def public_account(trader, observation):
    with trader._read() as db:
        portfolio = trader.portfolio(_db=db)
        account = trader._account(db)
        ledger_identity = digest({"cash":account["cash"],
            "positions":[dict(row) for row in db.execute("SELECT symbol,qty,cost FROM positions ORDER BY symbol")],
            "open_orders":[dict(row) for row in db.execute("SELECT * FROM orders WHERE status='open' ORDER BY id")]})
        fills = [dict(row) for row in db.execute("SELECT * FROM fills ORDER BY rowid")]
        from .risk import load
        risk = load(db,"risk_config")
        execution = load(db,"execution_config")
        max_age = account["max_age"]
        open_orders = [dict(row) for row in db.execute("SELECT * FROM orders WHERE status='open' ORDER BY id")]
        progress = {r["order_id"]:dict(r) for r in db.execute("SELECT * FROM order_progress")}
    if portfolio.get("currency") != "CNY" or portfolio.get("market") != "CN":
        raise ValueError("workflow requires a separate CN/CNY account")
    gap = age_seconds(portfolio["time"], observation["as_of"])
    if (portfolio.get("mode") != "live" and gap != 0) or (portfolio.get("mode") == "live" and not 0 <= gap <= 5):
        raise ValueError("frozen observation must match replay clock or be within five live seconds")
    fields = ("mode", "time", "cash", "initial_cash", "available_cash", "reserved_cash", "equity",
              "unrealized_pnl", "realized_pnl", "fees", "missing_marks", "currency", "market")
    result = {key: portfolio.get(key) for key in fields}
    result.update(snapshot_at=portfolio["time"], ledger_state_sha256=ledger_identity,
                  decision_binding="live bounded receipt gap" if gap else "same replay clock")
    result["constraints"] = {"risk":risk,"risk_sha256":digest(risk),"execution":execution,
                              "execution_sha256":digest(execution)}
    result["open_orders"] = [{**{k:o[k] for k in ("id","symbol","side","qty","limit_price","reserved_cash")},
                              "remaining_qty":progress.get(o["id"],{}).get("remaining_qty",o["qty"])} for o in open_orders]
    positions = []
    days = observation["calendar"]["trading_days"]
    from .cn import local_dt
    today = local_dt(portfolio["time"]).date().isoformat()
    from decimal import Decimal
    market_value, unrealized, missing = Decimal(0), Decimal(0), []
    for position in portfolio["positions"]:
        row = {k:position[k] for k in ("symbol", "qty", "cost", "market_value", "sellable_qty", "available_to_sell") if k in position}
        size, opened = 0, None
        for fill in fills:
            if fill["symbol"] != row["symbol"]:
                continue
            if size == 0:
                opened = local_dt(fill["time"]).date().isoformat()
            size += fill["qty"] * (1 if fill["side"] == "buy" else -1)
        row["holding_sessions"] = None if opened is None else sum(opened < day <= today for day in days)
        series = observation["series"].get(row["symbol"],[])
        if not series or age_seconds(observation["as_of"],series[-1]["asof"]) > max_age:
            row["market_value"] = None
            missing.append(row["symbol"])
        else:
            mark = money(number(series[-1]["price"],positive=True)*row["qty"])
            row["market_value"] = str(mark)
            market_value += mark
            unrealized += mark-number(row["cost"])
        positions.append(row)
    result["positions"] = positions
    result.update(equity=None if missing else str(number(result["cash"])+market_value),
                  unrealized_pnl=None if missing else str(unrealized),missing_marks=missing,
                  valuation_cutoff=observation["as_of"])
    return result


def run_cycle(trader, agent, observation, *, output, feedback=None):
    from .observation import validate_observation
    from .strategy import validate_research, validate_strategy, compile_intents, validate_intent
    observation = validate_observation(observation)
    folder = pathlib.Path(output)
    feedback = public_feedback(feedback)
    binding = {"version": "cn-cycle-v1", "observation_id": observation["observation_id"],
               "observation_sha256": digest(observation), "agent": agent.identity,
               "previous_review_sha256":digest(feedback),
               "account_identity": digest(str(pathlib.Path(trader.path).resolve()))}
    with run_lock(folder):
        artifact(folder / "manifest.json", binding)
        if (folder / "receipt.json").exists():
            receipt = read_artifact(folder / "receipt.json")
            if receipt.get("cycle_id") != digest(binding) or receipt.get("observation_id") != observation["observation_id"]:
                raise ValueError("cycle receipt binding mismatch")
            for name, sha in receipt.get("artifact_refs", {}).items():
                if name not in {"observation.json", "account.json", "model-output.json", "intent.json", "risk.json", "previous-review.json"} or digest(read_artifact(folder/name)) != sha:
                    raise ValueError("cycle receipt stage reference mismatch")
            if len(receipt.get("artifact_refs", {})) != 6:
                raise ValueError("cycle receipt is missing stage references")
            intent=read_artifact(folder/"intent.json")
            risk=read_artifact(folder/"risk.json")
            if receipt.get("intent_id")!=intent["intent_id"] or receipt.get("risk")!=risk or trader.intent_result(intent["intent_id"])!=risk:
                raise ValueError("historical cycle receipt does not match its ledger intent")
            # Later legitimate ledger evolution is allowed; damaged current state is not.
            phase(folder, "audit", lambda:checked_audit(trader))
            return receipt
        artifact(folder / "observation.json", observation)
        account_path = folder / "account.json"
        account = read_artifact(account_path) if account_path.exists() else artifact(account_path, public_account(trader, observation))
        # Feedback is from prior local receipts only; arbitrary documents are not sent.
        artifact(folder / "previous-review.json", feedback)
        model_path = folder / "model-output.json"
        model_account = {k:v for k,v in account.items() if k != "ledger_state_sha256"}
        decision = read_artifact(model_path) if model_path.exists() else phase(folder, "model", lambda:agent.decide(observation, model_account, feedback))
        if not isinstance(decision, dict) or set(decision) != {"research", "strategy"}:
            raise ValueError("agent output must contain only research and strategy")
        research = phase(folder, "research-validation", lambda:validate_research(decision["research"], observation))
        strategy = phase(folder, "strategy-validation", lambda:validate_strategy(decision["strategy"], observation, research))
        artifact(model_path, {"research": research, "strategy": strategy})
        intent = phase(folder, "intent", lambda:validate_intent(compile_intents(strategy, observation, account), observation, strategy))
        artifact(folder / "intent.json", intent)
        risk_path = folder / "risk.json"
        if not risk_path.exists():
            existing = trader.intent_result(intent["intent_id"])
            risk = existing if existing is not None else phase(folder, "risk", lambda:trader.submit_intent(intent, observation, account_state_sha256=account["ledger_state_sha256"]))
            artifact(risk_path, risk)
        else:
            risk = read_artifact(risk_path)
            if trader.intent_result(intent["intent_id"]) != risk:
                raise ValueError("saved risk receipt differs from ledger intent")
        matched = phase(folder, "match", trader.match)
        portfolio = phase(folder, "valuation", lambda:trader.portfolio(record=True))
        from .analytics import performance
        # Current ledger feedback survives a crash between fill and artifact write.
        report = performance(trader)
        order_ids = {order["id"] for order in risk.get("orders",[])}
        orders = [{key:order[key] for key in ("id","symbol","side","qty","status","filled_qty","remaining_qty") if key in order} for order in trader.history("orders",include_progress=True) if order["id"] in order_ids]
        fills = [{key:fill[key] for key in ("id","order_id","quote_id","symbol","side","qty","price","fee","time")} for fill in trader.history("fills") if fill["order_id"] in order_ids]
        waiting = [{"order_id":item["order_id"],"status":item["status"],"reason":item.get("reason","not filled")} for item in matched if "status" in item and item["order_id"] in order_ids]
        audit_summary = phase(folder, "audit", lambda:checked_audit(trader))
        receipt = {"version": "cn-cycle-review-v1", "cycle_id": digest(binding),
                   "observation_id": observation["observation_id"], "strategy_id": strategy["strategy_id"],
                   "intent_id": intent["intent_id"], "risk_status": risk["status"],
                   "risk": risk, "portfolio": portfolio, "report": report, "audit": audit_summary,
                   "execution": {"orders":orders,"fills":fills,"unfilled":waiting,
                                 "state":"risk_rejected" if risk["status"]=="rejected" else "filled" if fills else "holding" if not orders else "waiting_for_qualified_quote"},
                   "artifact_refs": {name:digest(read_artifact(folder/name)) for name in
                                     ("observation.json", "account.json", "model-output.json", "intent.json", "risk.json", "previous-review.json")},
                   "limitations": ["bounded local paper cycle, not exchange execution",
                                   "remote model is data-only; host administrator remains trusted"]}
        artifact(folder / "receipt.json", receipt)
        return receipt


def ingest_live_capture(trader, capture, universe):
    """Trusted fixed collector ingress; does not manufacture instrument evidence."""
    from .ashare import snapshot
    from .cn import require_available
    imported = []
    for ticker in universe:
        bound = json.loads(canonical(capture))
        with trader._read() as db:
            cn = trader._policy(db)
            if cn is None or trader._account(db)["mode"] != "live":
                raise ValueError("live capture ingestion requires live CN/CNY account")
            instrument = cn.instrument(db, ticker)
            require_available(instrument, trader._now(trader._account(db)))
        bound["instrument"] = {"symbol": ticker, "currency": "CNY", "currency_basis": "registered CN instrument",
                               "profile": instrument["profile"], "source": instrument["source"]}
        imported.append(trader._store_quote(**snapshot(bound, ticker)))
    return imported


def run_cycles(trader, agent, universe, *, output, rounds=1, documents=(), lookback=60,
               collect=False, captures=()):
    """Finite rounds at the current clock; no clock invention or background schedule."""
    if type(rounds) is not int or not 1 <= rounds <= 100:
        raise ValueError("cycle rounds must be 1..100")
    from .observation import build_observation
    if captures and len(captures) != rounds:
        raise ValueError("one externally collected capture per round required")
    live = trader.portfolio()["mode"] == "live"
    if live and not (collect or captures):
        raise ValueError("live rounds require fixed --collect worker or new externally delivered captures")
    if collect and (not live or not 1 <= len(universe) <= 3):
        raise ValueError("fixed live collector supports one to three qualified CN symbols")
    artifact(pathlib.Path(output)/"run-manifest.json",{"version":"cn-bounded-run-v1","universe":sorted(universe),
        "rounds":rounds,"lookback":lookback,"documents_sha256":digest(documents),"agent":agent.identity,
        "collect":collect,"capture_ids":[digest(c) for c in captures],"account_identity":digest(str(pathlib.Path(trader.path).resolve()))})
    results, feedback = [], None
    for index in range(rounds):
        folder = pathlib.Path(output)/f"cycle-{index:04d}"
        if live and not (folder/"receipt.json").exists():
            capture_path = folder/"market-capture.json"
            if capture_path.exists():
                capture = read_artifact(capture_path)
            elif captures:
                capture = artifact(capture_path, captures[index])
            else:
                from .ashare import AshareMCP
                capture = artifact(capture_path, phase(folder,"collection",lambda:AshareMCP().call("mercury_get_realtime_quote", {"symbols": list(universe)})))
            if any(digest(capture) == result.get("market_capture_sha256") for result in results):
                raise ValueError("next round requires a new market capture, not a repeated envelope")
            phase(folder,"ingress-validation",lambda:ingest_live_capture(trader,capture,universe))
        observation = read_artifact(folder/"observation.json") if (folder/"observation.json").exists() else build_observation(trader, universe, documents=documents, lookback=lookback)
        result = run_cycle(trader, agent, observation, output=folder, feedback=feedback)
        if live:
            result = {**result, "market_capture_sha256": digest(read_artifact(folder/"market-capture.json"))}
        results.append(result)
        feedback = result
    return {"rounds": len(results), "cycles": results, "background_schedule": False}
