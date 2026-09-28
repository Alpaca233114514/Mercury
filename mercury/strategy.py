"""Strict JSON schemas and a tiny Decimal-only CN signal/intent compiler.

No model-generated code, evaluation, network, database mutation or execution price.
Signals use unique observation rows, never imply an N-day frequency for snapshots.
"""

import json
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from .common import age_seconds, canonical, digest, number, timestamp
from .cn import cn_symbol, local_dt
from .observation import (decimal, exact, observation_references, sha, text,
                          validate_observation, _identity, _seal, _time)

RESEARCH_VERSION = "cn-research-v1"
STRATEGY_VERSION = "cn-strategy-v1"
INTENT_VERSION = "cn-trade-intent-v1"


def _refs(refs, obs):
    if (not isinstance(refs, list) or not refs or len(refs) > 1000 or
            any(not isinstance(r, str) for r in refs) or len(set(refs)) != len(refs)):
        raise ValueError("nonempty unique evidence references required")
    known = observation_references(obs)
    if any(not isinstance(r, str) or r not in known for r in refs):
        raise ValueError("evidence reference absent from frozen observation")


def _binding(value, obs, version):
    canonical(value)
    if value["version"] != version or value["observation_id"] != obs["observation_id"] or _time(value["as_of"]) != obs["as_of"]:
        raise ValueError("version, observation or decision time mismatch")


def _holding(value):
    exact(value, {"max_sessions"}, "holding")
    if type(value["max_sessions"]) is not int or not 1 <= value["max_sessions"] <= 252:
        raise ValueError("holding max_sessions must be 1..252")


def _invalidation(value, obs):
    exact(value, {"expires_at", "max_quote_age_seconds", "min_signal"}, "invalidation")
    expiry = _time(value["expires_at"])
    if expiry <= obs["as_of"] or age_seconds(expiry, obs["as_of"]) > 366 * 86400:
        raise ValueError("strategy expiry must be after decision and within 366 days")
    if type(value["max_quote_age_seconds"]) is not int or not 1 <= value["max_quote_age_seconds"] <= 86400:
        raise ValueError("quote age limit must be 1..86400 seconds")
    decimal(value["min_signal"])


def validate_research(research, observation):
    obs = validate_observation(observation)
    exact(research, {"version", "research_id", "observation_id", "as_of", "hypotheses"}, "research")
    _binding(research, obs, RESEARCH_VERSION)
    hypotheses = research["hypotheses"]
    if not isinstance(hypotheses, list) or not 1 <= len(hypotheses) <= 20:
        raise ValueError("research requires 1..20 hypotheses")
    seen = set()
    for hypothesis in hypotheses:
        exact(hypothesis, {"hypothesis_id", "statement", "evidence_ids", "invalidation"}, "hypothesis")
        name = text(hypothesis["hypothesis_id"], limit=64)
        if name in seen:
            raise ValueError("duplicate hypothesis identifier")
        seen.add(name)
        text(hypothesis["statement"])
        text(hypothesis["invalidation"])
        _refs(hypothesis["evidence_ids"], obs)
    _identity(research, "research_id")
    return json.loads(canonical(research))


def validate_strategy(strategy, observation, research=None):
    obs = validate_observation(observation)
    exact(strategy, {"version", "strategy_id", "observation_id", "research_id", "as_of", "universe", "signals",
                     "top_k", "allocation", "rebalance", "holding", "invalidation", "reason", "evidence_ids"}, "strategy")
    _binding(strategy, obs, STRATEGY_VERSION)
    sha(strategy["research_id"])
    if research is not None and validate_research(research, obs)["research_id"] != strategy["research_id"]:
        raise ValueError("strategy references different research")
    if (not isinstance(strategy["universe"], list) or not strategy["universe"] or
            any(not isinstance(s, str) for s in strategy["universe"]) or
            strategy["universe"] != sorted(set(strategy["universe"])) or not set(strategy["universe"]).issubset(obs["universe"])):
        raise ValueError("strategy universe must be explicit unique observation subset")
    if type(strategy["top_k"]) is not int or not 1 <= strategy["top_k"] <= len(strategy["universe"]):
        raise ValueError("top_k outside universe size")
    signals = strategy["signals"]
    if not isinstance(signals, list) or not 1 <= len(signals) <= 3:
        raise ValueError("DSL requires 1..3 allowed signals")
    seen, weights = set(), Decimal(0)
    for signal in signals:
        exact(signal, {"name", "window", "weight"}, "signal")
        if not isinstance(signal["name"], str) or signal["name"] not in {"momentum", "volatility", "sma"} or signal["name"] in seen:
            raise ValueError("signal outside restricted DSL or duplicate")
        seen.add(signal["name"])
        if type(signal["window"]) is not int or not 2 <= signal["window"] <= 9999:
            raise ValueError("signal window must count 2..9999 observations")
        weights += decimal(signal["weight"], positive=True)
    if weights != 1:
        raise ValueError("signal weights must sum exactly to one")
    exact(strategy["allocation"], {"gross_weight", "weighting"}, "allocation")
    if strategy["allocation"]["weighting"] != "equal" or not 0 <= decimal(strategy["allocation"]["gross_weight"]) <= 1:
        raise ValueError("only unlevered equal weighting is supported")
    exact(strategy["rebalance"], {"every_sessions", "anchor_day"}, "rebalance")
    every = strategy["rebalance"]["every_sessions"]
    if type(every) is not int or not 1 <= every <= 252:
        raise ValueError("rebalance interval must be 1..252 verified sessions")
    anchor = strategy["rebalance"]["anchor_day"]
    if not isinstance(anchor, str) or anchor not in obs["calendar"]["trading_days"]:
        raise ValueError("rebalance anchor must be an observed calendar trading day")
    _holding(strategy["holding"])
    _invalidation(strategy["invalidation"], obs)
    text(strategy["reason"])
    _refs(strategy["evidence_ids"], obs)
    _identity(strategy, "strategy_id")
    return json.loads(canonical(strategy))


def validate_intent(intent, observation, strategy=None):
    obs = validate_observation(observation)
    exact(intent, {"version", "intent_id", "as_of", "observation_id", "strategy_id", "reason", "holding",
                   "invalidation", "orders", "evidence_ids"}, "trade intent")
    _binding(intent, obs, INTENT_VERSION)
    sha(intent["strategy_id"])
    if strategy is not None:
        validated = validate_strategy(strategy, obs)
        if (intent["strategy_id"] != validated["strategy_id"] or intent["holding"] != validated["holding"] or
                intent["invalidation"] != validated["invalidation"]):
            raise ValueError("intent does not match strategy/versioned constraints")
    _holding(intent["holding"])
    _invalidation(intent["invalidation"], obs)
    text(intent["reason"])
    _refs(intent["evidence_ids"], obs)
    if not isinstance(intent["orders"], list) or len(intent["orders"]) > 1000:
        raise ValueError("orders must be a bounded list; empty means hold")
    if intent["orders"] and not obs["calendar"]["is_open"]:
        raise ValueError("closed-market observation permits hold only")
    seen = set()
    for order in intent["orders"]:
        exact(order, {"symbol", "side", "qty", "limit_price", "target_weight"}, "intent order")
        ticker = cn_symbol(order["symbol"])
        if ticker not in obs["universe"] or ticker in seen or order["side"] not in {"buy", "sell"}:
            raise ValueError("order identity/direction/duplicate invalid")
        seen.add(ticker)
        if type(order["qty"]) is not int or not 1 <= order["qty"] <= 100000000:
            raise ValueError("order quantity must be positive integer")
        limit = decimal(order["limit_price"], positive=True)
        if not 0 <= decimal(order["target_weight"]) <= 1:
            raise ValueError("target weight must be 0..1")
        instrument = obs["instruments"].get(ticker)
        state = obs["status"].get(ticker)
        if instrument is None or state is None or state["suspended"] or not obs["series"][ticker]:
            raise ValueError("order requires known tradable metadata and quote")
        if age_seconds(obs["as_of"], obs["series"][ticker][-1]["asof"]) > intent["invalidation"]["max_quote_age_seconds"]:
            raise ValueError("intent references quote older than strategy validity limit")
        lot = instrument["lot"]
        if limit % decimal(lot["tick"], positive=True) != 0 or order["qty"] > lot["max_limit_qty"]:
            raise ValueError("order price tick/quantity ceiling violation")
        if order["side"] == "buy" and (order["qty"] < lot["min_qty"] or (order["qty"] - lot["min_qty"]) % lot["qty_step"]):
            raise ValueError("buy lot minimum/increment violation")
        if (state["limit_down"] is not None and limit < decimal(state["limit_down"], positive=True) or
                state["limit_up"] is not None and limit > decimal(state["limit_up"], positive=True)):
            raise ValueError("order limit outside sourced daily price bands")
    _identity(intent, "intent_id")
    return json.loads(canonical(intent))


def _score(rows, signals, trace=None):
    if len({r["frequency"] for r in rows}) != 1 or rows[0]["frequency"] == "unknown":
        raise ValueError("signal input frequency mixed or unverified")
    prices = [decimal(r["price"], positive=True) for r in rows]
    score = Decimal(0)
    for signal in signals:
        window = signal["window"]
        need = window if signal["name"] == "sma" else window + 1
        if len(prices) < need:
            raise ValueError("insufficient frozen observation rows for signal")
        tail = prices[-need:]
        if signal["name"] == "momentum":
            value = tail[-1] / tail[0] - 1
        elif signal["name"] == "sma":
            value = tail[-1] / (sum(tail) / window) - 1
        else:
            returns = [b / a - 1 for a, b in zip(tail, tail[1:])]
            mean = sum(returns) / len(returns)
            value = -(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)).sqrt()
        score += value * decimal(signal["weight"], positive=True)
        if trace is not None:
            trace.append({"name": signal["name"], "window": window, "weight": signal["weight"],
                          "quote_ids": [r["quote_id"] for r in rows[-need:]],
                          "prices": [str(p) for p in tail], "value": str(value),
                          "weighted_value": str(value * decimal(signal["weight"], positive=True)),
                          "running_score": str(score)})
    return score


def compile_intents(strategy, observation, account_snapshot, *, trace=None):
    """Compile target allocation into explicit lot-valid limits; hard risk owns approval.

    Existing positions require holding_sessions from the controlled runner. Orders
    never spend assumed sale proceeds; fees, cash and portfolio caps are independently
    approved atomically by PaperTrader. There is no silent risk clipping here.
    """
    obs = validate_observation(observation)
    strategy = validate_strategy(strategy, obs)
    if trace is not None:
        from .records import code_identity
        trace.update(version="mercury-calculation-v1", observation_id=obs["observation_id"],
                     strategy_id=strategy["strategy_id"], account_sha256=digest(account_snapshot),
                     code=code_identity(), signals=[], ranking=[], targets=[], status="started")
    portfolio = account_snapshot
    if not isinstance(portfolio, dict) or portfolio.get("currency") != "CNY" or portfolio.get("missing_marks") or portfolio.get("equity") is None:
        raise ValueError("compiler requires CNY portfolio with complete valuation")
    account_time = timestamp(portfolio.get("time"))
    lag = age_seconds(account_time, obs["as_of"])
    if (portfolio.get("mode") == "live" and not 0 <= lag <= 5) or (portfolio.get("mode") != "live" and lag != 0):
        raise ValueError("portfolio must match replay cutoff or be within 5 seconds after live cutoff")
    equity = decimal(portfolio["equity"])
    if equity < 0:
        raise ValueError("portfolio equity cannot be negative")
    positions = {}
    for p in portfolio.get("positions", []):
        ticker = cn_symbol(p.get("symbol"))
        if ticker not in obs["universe"] or ticker in positions or type(p.get("qty")) is not int or p["qty"] < 0:
            raise ValueError("positions must be nonnegative unique observation instruments")
        if p["qty"]:
            if type(p.get("holding_sessions")) is not int or p["holding_sessions"] < 0:
                raise ValueError("existing position holding-session evidence required")
            if type(p.get("available_to_sell")) is not int or not 0 <= p["available_to_sell"] <= p["qty"]:
                raise ValueError("verified sellable position quantity required")
        positions[ticker] = p
    orders, reason = [], strategy["reason"]
    pending = portfolio.get("open_orders", [])
    if not isinstance(pending, list) or len(pending) > 1000:
        raise ValueError("open orders must be a bounded list from the controlled account")
    for order in pending:
        if (not isinstance(order, dict) or order.get("side") not in {"buy", "sell"} or
                type(order.get("remaining_qty")) is not int or order["remaining_qty"] <= 0):
            raise ValueError("open order requires explicit positive remaining quantity")
        cn_symbol(order.get("symbol"))
    calendar = obs["calendar"]
    anchor = calendar["trading_days"].index(strategy["rebalance"]["anchor_day"])
    expired_hold = any(p.get("holding_sessions", 0) >= strategy["holding"]["max_sessions"] for p in positions.values())
    due = calendar["is_open"] and (expired_hold or (len(calendar["trading_days"]) - 1 - anchor) % strategy["rebalance"]["every_sessions"] == 0)
    if trace is not None:
        trace.update(equity=str(equity), rebalance={"market_open": calendar["is_open"],
                     "expired_hold": expired_hold, "due": due, "pending_orders": len(pending)})
    if pending:
        reason += "; hold: outstanding order remainder; cancel or finish it before rebalancing"
    elif not due:
        reason += "; hold: closed market or rebalance not due"
    else:
        scored = []
        for ticker in strategy["universe"]:
            rows = obs["series"][ticker]
            if (not rows or ticker not in obs["instruments"] or ticker not in obs["status"] or
                    obs["status"][ticker]["suspended"] or
                    age_seconds(obs["as_of"], rows[-1]["asof"]) > strategy["invalidation"]["max_quote_age_seconds"]):
                raise ValueError("strategy instrument has missing/stale/untradable frozen data")
            details = [] if trace is not None else None
            if trace is not None:
                trace["signals"].append({"symbol": ticker, "values": details})
            scored.append((_score(rows, strategy["signals"], details), ticker))
        scored.sort(key=lambda item: (-item[0], item[1]))
        # Expired holdings leave the ranking before top_k; never immediately rebuy.
        selected = [ticker for score, ticker in scored if score >= decimal(strategy["invalidation"]["min_signal"]) and
                    positions.get(ticker, {}).get("holding_sessions", 0) < strategy["holding"]["max_sessions"]][:strategy["top_k"]]
        gross = decimal(strategy["allocation"]["gross_weight"])
        weight = gross / len(selected) if selected else Decimal(0)
        if trace is not None:
            trace["ranking"] = [{"symbol": ticker, "score": str(score), "rank": i+1,
                "selected": ticker in selected, "reason": "selected" if ticker in selected else
                "below_min_signal" if score < decimal(strategy["invalidation"]["min_signal"]) else
                "holding_expired" if positions.get(ticker, {}).get("holding_sessions", 0) >= strategy["holding"]["max_sessions"] else "outside_top_k"}
                for i, (score, ticker) in enumerate(scored)]
        for ticker in sorted(set(strategy["universe"]) | set(positions)):
            rows = obs["series"][ticker]
            if not rows:
                raise ValueError("position target requires available quote")
            price = decimal(rows[-1]["price"], positive=True)
            lot = obs["instruments"].get(ticker, {}).get("lot")
            if lot is None:
                raise ValueError("position target requires verified lot profile")
            target_weight = weight if ticker in selected else Decimal(0)
            target = int((equity * target_weight / price).to_integral_value(rounding=ROUND_FLOOR))
            unrounded_target = str(equity * target_weight / price)
            floored_target = target
            minimum, step = lot["min_qty"], lot["qty_step"]
            target = 0 if target < minimum else minimum + ((target - minimum) // step) * step
            current = positions.get(ticker, {}).get("qty", 0)
            difference = target - current
            detail = {"symbol": ticker, "equity": str(equity), "target_weight": str(target_weight),
                      "quote_id": rows[-1]["quote_id"], "reference_price": str(price),
                      "unrounded_target": unrounded_target, "floored_target": floored_target,
                      "lot": lot, "rounded_target": target, "current_qty": current,
                      "difference": difference, "available_to_sell": positions.get(ticker, {}).get("available_to_sell"),
                      "status": "target_unchanged" if not difference else "started"}
            if trace is not None:
                trace["targets"].append(detail)
            if not difference:
                continue
            side = "buy" if difference > 0 else "sell"
            qty = abs(difference)
            if side == "buy":
                if qty < minimum:
                    detail["status"] = "below_minimum_lot"
                    continue  # Explicit lot rounding, not risk-limit clipping.
                qty = minimum + ((qty - minimum) // step) * step
            else:
                available = positions[ticker]["available_to_sell"]
                if qty > available:
                    raise ValueError("rebalance requires T+1/reserved unavailable shares")
                regular = qty >= lot["sell_min_qty"] and (qty - lot["sell_min_qty"]) % lot["sell_qty_step"] == 0
                if not regular and qty != available:
                    raise ValueError("rebalance sell quantity requires unsupported partial odd lot")
            orders.append({"symbol": ticker, "side": side, "qty": qty, "limit_price": str(price), "target_weight": str(target_weight)})
            detail.update(status="intent", side=side, qty=qty, limit_price=str(price))
        orders.sort(key=lambda o: (o["side"] == "buy", o["symbol"]))
        if not orders:
            reason += "; hold: target unchanged or below verified minimum lot"
    body = {"version": INTENT_VERSION, "as_of": obs["as_of"], "observation_id": obs["observation_id"],
            "strategy_id": strategy["strategy_id"], "reason": reason, "holding": strategy["holding"],
            "invalidation": strategy["invalidation"], "orders": orders, "evidence_ids": strategy["evidence_ids"]}
    result = validate_intent(_seal(body, "intent_id"), obs, strategy)
    if trace is not None:
        trace.update(status="completed", intent_id=result["intent_id"], reason=reason,
                     signals_status="executed" if trace["signals"] else "not_executed")
    return result


compile_intent = compile_intents
