"""Deterministic CNY investment limits; no model code or network execution."""

import json
from decimal import Decimal

from .common import canonical, digest, money, number, timestamp
from .cn import local_dt

V2_SCHEMA = """
CREATE TABLE ledger_version(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
INSERT INTO ledger_version VALUES(1,2);
CREATE TABLE risk_config(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE execution_config(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE order_progress(order_id TEXT PRIMARY KEY, filled_qty INTEGER NOT NULL,
 remaining_qty INTEGER NOT NULL, last_quote_asof TEXT);
CREATE TABLE quote_capacity(symbol TEXT NOT NULL, asof TEXT NOT NULL, consumed_qty INTEGER NOT NULL,
 PRIMARY KEY(symbol,asof));
CREATE TABLE risk_days(day TEXT PRIMARY KEY, baseline_equity TEXT NOT NULL, basis TEXT NOT NULL);
CREATE TABLE risk_state(id INTEGER PRIMARY KEY CHECK(id=1), time TEXT NOT NULL, equity TEXT NOT NULL);
CREATE TABLE intents(intent_id TEXT PRIMARY KEY, payload TEXT NOT NULL, observation TEXT NOT NULL,
 result TEXT NOT NULL);
CREATE TABLE order_intents(order_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, target_weight TEXT NOT NULL);
CREATE TABLE risk_decisions(id TEXT PRIMARY KEY, time TEXT NOT NULL, payload TEXT NOT NULL);
"""

DEFAULT_EXECUTION = {"version": "paper-execution-v1", "spread_bps": "0",
                     "latency_seconds": 0, "max_fill_qty": None,
                     "capacity_basis": "symbol-provider-asof", "commission_basis": "order-cumulative"}


def v2(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='ledger_version'").fetchone())


def validate_policy(value):
    required = {"version", "currency", "max_position_weight", "max_order_notional",
                "max_daily_turnover", "max_daily_loss"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("risk policy requires exactly version,currency and four investment limits")
    if value["version"] != "cn-risk-v1" or value["currency"] != "CNY":
        raise ValueError("only cn-risk-v1 CNY investment policies are supported")
    result = dict(value)
    for key in required - {"version", "currency"}:
        val = number(value[key], positive=True)
        if key in {"max_position_weight", "max_daily_loss"} and val > 1:
            raise ValueError("risk weight/loss limits must be <= 1")
        result[key] = str(val)
    return result


def validate_execution(value):
    if value is None:
        return dict(DEFAULT_EXECUTION)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_EXECUTION):
        raise ValueError("unknown execution policy fields")
    result = dict(DEFAULT_EXECUTION, **value)
    if (result["version"] != "paper-execution-v1" or
            result["capacity_basis"] != "symbol-provider-asof" or
            result["commission_basis"] != "order-cumulative"):
        raise ValueError("unsupported execution model")
    spread = number(result["spread_bps"])
    if not 0 <= spread <= 100:
        raise ValueError("spread_bps must be 0..100")
    result["spread_bps"] = str(spread)
    if type(result["latency_seconds"]) is not int or not 0 <= result["latency_seconds"] <= 86400:
        raise ValueError("latency_seconds must be an integer 0..86400")
    cap = result["max_fill_qty"]
    if cap is not None and (type(cap) is not int or not 1 <= cap <= 1000000000):
        raise ValueError("max_fill_qty must be null or positive integer <= 1e9")
    return result


def load(db, table):
    if not v2(db):
        return None
    row = db.execute(f"SELECT payload,hash FROM {table} WHERE id=1").fetchone()
    if row is None:
        return None
    result = json.loads(row[0])
    if digest(result) != row[1]:
        raise ValueError("immutable policy hash mismatch")
    return result


def remaining(db, order):
    if not v2(db):
        return order["qty"]
    row = db.execute("SELECT remaining_qty FROM order_progress WHERE order_id=?", (order["id"],)).fetchone()
    if row is None:
        raise ValueError("order progress missing")
    return row[0]


def pending(db, *, exclude=None):
    result = []
    for row in db.execute("SELECT * FROM orders WHERE status='open'"):
        order = dict(row)
        if order["id"] != exclude:
            order["qty"] = remaining(db, order)
            result.append(order)
    return result


def observation_binding(trader, db, obs, now, *, max_quote_age_seconds):
    """Bind a frozen projection to this account's immutable source records."""
    from .common import age_seconds
    account, cn = trader._account(db), trader._policy(db)
    elapsed = age_seconds(now, obs["as_of"])
    age_limit = min(account["max_age"], max_quote_age_seconds)
    if (account["mode"] == "live" and not 0 <= elapsed <= age_limit) or (account["mode"] != "live" and elapsed != 0):
        raise ValueError("frozen observation does not match current account decision clock")
    if obs["calendar"]["sha256"] != digest(cn.calendar) or obs["rules"]["sha256"] != digest(cn.rules):
        raise ValueError("observation market configuration differs from execution account")
    for ticker, rows in obs["series"].items():
        for item in rows:
            q = db.execute("SELECT * FROM quotes WHERE id=?", (item["quote_id"],)).fetchone()
            if q is None:
                raise ValueError("observation quote is absent from execution account")
            evidence = json.loads(q["evidence"])
            expected = {"quote_id": q["id"], "asof": timestamp(q["asof"]),
                        "available_at": timestamp(evidence.get("available_at", q["asof"])),
                        "price": str(number(q["price"])), "kind": q["kind"], "source_hash": q["evidence_hash"],
                        "frequency": evidence.get("frequency", "snapshot" if q["kind"] == "mcp" else "fixture" if q["kind"] == "fixture" else "unknown")}
            if item != expected or q["symbol"] != ticker or q["currency"] != "CNY":
                raise ValueError("observation quote differs from execution account evidence")
    for ticker, item in obs["instruments"].items():
        from .observation import LOT_FIELDS
        inst = cn.instrument(db, ticker)
        profile = cn.profile(inst)
        expected = {"profile": inst["profile"], "available_at": timestamp(inst["source"]["available_at"]),
                    "source_id": digest(inst["source"]), "source_hash": digest(inst),
                    "lot": {key: profile[key] for key in LOT_FIELDS}}
        if item != expected:
            raise ValueError("observation instrument differs from execution account")
    for ticker, item in obs["status"].items():
        state = cn.status(db, ticker, obs["as_of"])
        expected = {k: state[k] for k in ("day", "suspended", "risk_warning", "listing_session", "reference_price", "limit_up", "limit_down")}
        expected.update(available_at=timestamp(state["source"]["available_at"]), source_id=digest(state["source"]), source_hash=digest(state))
        if item != expected:
            raise ValueError("observation daily status differs from execution account")


def check(trader, db, *, order, price, qty, fee, phase, at=None):
    """Return a reproducible decision; caller commits rejection evidence separately.

    Existing sell proceeds are never budgeted before they fill. Investment loss
    limits block buys, while sells remain subject to all other declared limits.
    """
    policy = load(db, "risk_config")
    if policy is None:
        return None
    account = trader._account(db)
    now = at or trader._now(account)
    portfolio = trader.portfolio(_db=db, _at=now)
    evidence = {"phase": phase, "policy_hash": digest(policy), "order": dict(order),
                "price": str(price), "qty": qty, "fee": str(fee), "time": now,
                "loss_action": "block_buys_allow_sells", "errors": []}
    errors = evidence["errors"]
    if portfolio["equity"] is None:
        errors.append("risk valuation missing/stale")
        return evidence
    equity = number(portfolio["equity"])
    if equity <= 0:
        errors.append("risk equity must be positive")
        return evidence
    day = local_dt(now).date().isoformat()
    baseline = db.execute("SELECT * FROM risk_days WHERE day=?", (day,)).fetchone()
    if baseline is None:
        previous = None
        for row in db.execute("SELECT payload FROM snapshots ORDER BY seq DESC"):
            item = json.loads(row[0])
            if local_dt(item["time"]).date().isoformat() < day and item.get("equity") is not None:
                previous = item
                break
        prior_state = db.execute("SELECT * FROM risk_state WHERE id=1").fetchone()
        if prior_state and local_dt(prior_state["time"]).date().isoformat() < day:
            if previous is None or prior_state["time"] > previous["time"]:
                previous = dict(time=prior_state["time"], equity=prior_state["equity"])
        if previous:
            base, basis = number(previous["equity"]), "prior recorded complete valuation"
        elif not portfolio["positions"]:
            base, basis = equity, "known cash-only daily baseline"
        else:
            base, basis = Decimal(0), "baseline_missing"
        db.execute("INSERT INTO risk_days VALUES(?,?,?)", (day, str(base), basis))
    else:
        base, basis = number(baseline["baseline_equity"]), baseline["basis"]
    evidence.update(day=day, equity=str(equity), baseline_equity=str(base), baseline_basis=basis)
    db.execute("INSERT INTO risk_state VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET time=excluded.time,equity=excluded.equity", (now, str(equity)))
    if base <= 0:
        if order["side"] == "buy":
            errors.append("baseline_missing: new buys require prior complete valuation")
            return evidence
        # Reductions retain single-order and turnover caps using current equity;
        # the zero daily baseline stays persisted so later buys cannot reset it.
        base = equity
        evidence["reduction_budget_basis"] = "current complete valuation; buys remain blocked"
    notional = money(price * qty)
    others = pending(db, exclude=order.get("id"))
    # At fill, include this order's not-yet-executed remainder too.
    candidate = dict(order, qty=qty, limit_price=str(price))
    if phase == "fill":
        left = remaining(db, order) - qty
        if left:
            others.append(dict(order, qty=left))
    commitments = others + [candidate]
    turnover = sum((number(f["notional"]) + number(f["fee"]) for f in db.execute("SELECT * FROM fills")
                    if local_dt(f["time"]).date().isoformat() == day), Decimal(0))
    pending_turnover = Decimal(0)
    exposure = {p["symbol"]: number(p["market_value"]) for p in portfolio["positions"]}
    pending_fees, adverse = Decimal(0), Decimal(0)
    estimated_by_order = {}
    cn = trader._policy(db)
    for item in commitments:
        quote = trader._quote(db, item["symbol"], now, account)
        mark = number(quote["price"])
        worst = max(number(item["limit_price"]), mark)
        amount = money(worst * item["qty"])
        previous_amount, previous_charge = estimated_by_order.get(item.get("id"), (Decimal(0), Decimal(0)))
        cumulative_charge = trader._estimated_fee(db, cn, item, previous_amount + amount)
        charge = cumulative_charge - previous_charge
        estimated_by_order[item.get("id")] = (previous_amount + amount, cumulative_charge)
        pending_turnover += amount + charge
        if item["side"] == "buy":
            exposure[item["symbol"]] = exposure.get(item["symbol"], Decimal(0)) + amount
            pending_fees += charge
            adverse += max(Decimal(0), worst - mark) * item["qty"]
    projected_equity = equity - pending_fees - adverse
    evidence.update(notional=str(notional), turnover=str(turnover), committed_turnover=str(pending_turnover),
                    projected_equity=str(projected_equity), exposure={k: str(v) for k, v in exposure.items()})
    if notional + fee > number(policy["max_order_notional"]):
        errors.append("single order CNY amount including fees exceeds policy")
    committed_amount, committed_fee = estimated_by_order.get(order.get("id"), (notional, fee))
    prior_paid = sum((number(f["notional"]) + number(f["fee"]) for f in db.execute("SELECT * FROM fills WHERE order_id=?", (order.get("id", ""),))), Decimal(0))
    if prior_paid + committed_amount + committed_fee > number(policy["max_order_notional"]):
        errors.append("original order including remaining commitment exceeds CNY cap")
    # Original-order total cap remains enforced after partial executions.
    if phase == "fill":
        paid = sum((number(f["notional"]) + number(f["fee"]) for f in db.execute("SELECT * FROM fills WHERE order_id=?", (order["id"],))), Decimal(0))
        if paid + notional + fee > number(policy["max_order_notional"]):
            errors.append("cumulative order CNY amount including fees exceeds policy")
    if turnover + pending_turnover > base * number(policy["max_daily_turnover"]):
        errors.append("daily turnover including fees/open orders exceeds policy")
    if order["side"] == "buy":
        if order.get("target_weight") is not None and number(order["target_weight"]) > number(policy["max_position_weight"]):
            errors.append("intent target weight exceeds investment policy")
        if projected_equity <= 0 or any(value > projected_equity * number(policy["max_position_weight"]) for value in exposure.values()):
            errors.append("single security weight including open buys exceeds policy")
        if projected_equity < base * (1 - number(policy["max_daily_loss"])):
            errors.append("daily loss limit blocks new buys")
    return evidence
