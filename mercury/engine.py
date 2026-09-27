"""Transactional single-account ledger; Decimal math, no network or real execution."""

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path

from .common import age_seconds, canonical, digest, money, number, symbol, timestamp, utcnow

SCHEMA = """
CREATE TABLE account(id INTEGER PRIMARY KEY CHECK(id=1), initial_cash TEXT NOT NULL,
 cash TEXT NOT NULL, fee_bps TEXT NOT NULL, slippage_bps TEXT NOT NULL,
 mode TEXT NOT NULL, clock TEXT NOT NULL, max_age INTEGER NOT NULL, currency TEXT NOT NULL);
CREATE TABLE quotes(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, price TEXT NOT NULL,
 asof TEXT NOT NULL, received_at TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
 currency TEXT NOT NULL, evidence TEXT NOT NULL, evidence_hash TEXT NOT NULL);
CREATE INDEX quote_lookup ON quotes(symbol,asof,received_at);
CREATE TABLE positions(symbol TEXT PRIMARY KEY, qty INTEGER NOT NULL, cost TEXT NOT NULL);
CREATE TABLE orders(id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, symbol TEXT NOT NULL,
 side TEXT NOT NULL, qty INTEGER NOT NULL, limit_price TEXT, status TEXT NOT NULL,
 created_at TEXT NOT NULL, reserved_cash TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE fills(id TEXT PRIMARY KEY, order_id TEXT UNIQUE NOT NULL, quote_id TEXT NOT NULL,
 symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, price TEXT NOT NULL,
 notional TEXT NOT NULL, fee TEXT NOT NULL, realized_pnl TEXT NOT NULL, time TEXT NOT NULL);
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE snapshots(seq INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, payload TEXT NOT NULL);
"""


class PaperTrader:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_file():
            raise ValueError("account does not exist; run init first")

    @classmethod
    def create(cls, path, *, cash="100000", fee_bps="5", slippage_bps="2", mode="demo",
               start=None, max_age=300, currency=None, market="US", calendar=None, minimum_commission="5",
               risk_policy=None, execution_policy=None):
        from .risk import validate_policy, validate_execution, V2_SCHEMA
        risk_value = validate_policy(risk_policy) if risk_policy is not None else None
        execution_value = validate_execution(execution_policy)
        if risk_value is not None and market != "CN":
            raise ValueError("investment policy requires an isolated CN/CNY account")
        cash, fee, slip = money(cash), number(fee_bps), number(slippage_bps)
        if cash <= 0 or not 0 <= fee <= 100 or not 0 <= slip <= 100:
            raise ValueError("cash must be positive; fee/slippage must be 0..100 bps")
        currency = currency or ("CNY" if market == "CN" else "USD")
        if mode not in {"demo", "replay", "live"} or (market, currency) not in {("US", "USD"), ("CN", "CNY")}:
            raise ValueError("supported modes: demo/replay/live; isolated US/USD or CN/CNY accounts required")
        cn_config = None
        if market == "CN":
            from .cn import create_config
            cn_config = create_config(calendar, fee, minimum_commission)
        elif calendar is not None:
            raise ValueError("CN calendar cannot be attached to a US account")
        if type(max_age) is not int or not 1 <= max_age <= 86400:
            raise ValueError("max_age must be 1..86400 seconds")
        if mode != "live" and start is None:
            raise ValueError("demo/replay requires explicit start time")
        if mode == "live" and start is not None:
            raise ValueError("live clock cannot be supplied")
        clock = utcnow() if mode == "live" else timestamp(start)
        if cn_config is not None:
            from .cn import CNMarket
            CNMarket(cn_config).day(clock, require_open=False)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation preserves all existing accounts, including empty files.
        with path.open("xb"):
            pass
        obj = cls(path)
        with closing(obj._connect()) as db:
            with db:
                db.executescript(SCHEMA.replace("order_id TEXT UNIQUE NOT NULL", "order_id TEXT NOT NULL"))
                db.executescript(V2_SCHEMA)
                db.execute("INSERT INTO execution_config VALUES(1,?,?)", (canonical(execution_value), digest(execution_value)))
                if cn_config is not None:
                    from .cn import CN_SCHEMA
                    db.executescript(CN_SCHEMA)
                    db.execute("INSERT INTO market_config VALUES(1,?,?)", (canonical(cn_config), digest(cn_config)))
                db.execute("INSERT INTO account VALUES(1,?,?,?,?,?,?,?,?)",
                           (str(cash), str(cash), str(fee), str(slip), mode, clock, max_age, currency))
                initial = dict(cash=str(cash), fee_bps=str(fee), slippage_bps=str(slip), mode=mode, currency=currency, max_age=max_age)
                if cn_config is not None:
                    initial["market_config"] = cn_config
                initial.update(schema_version=2, execution_policy=execution_value)
                obj._event(db, clock, "init", initial)
        if risk_value is not None:
            obj.configure_risk(risk_value)
        return obj

    def configure_risk(self, policy):
        """Operator initialization only; once configured it cannot be changed by intent."""
        from .risk import validate_policy, v2
        value = validate_policy(policy)
        with self._tx() as db:
            if not v2(db) or self._account(db)["currency"] != "CNY":
                raise ValueError("risk configuration requires a new v2 CNY account; no legacy migration")
            if db.execute("SELECT 1 FROM risk_config").fetchone() or db.execute("SELECT 1 FROM orders").fetchone() or db.execute("SELECT 1 FROM fills").fetchone():
                raise ValueError("risk policy is immutable and must precede all orders/fills")
            db.execute("INSERT INTO risk_config VALUES(1,?,?)", (canonical(value), digest(value)))
            self._event(db, self._now(self._account(db)), "risk_policy", value)
        return {"policy": value, "policy_hash": digest(value)}

    def get_intent_result(self, intent_id):
        from .risk import v2
        with self._read() as db:
            if not v2(db):
                return None
            row = db.execute("SELECT result FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            return json.loads(row[0]) if row else None

    intent_result = get_intent_result

    @staticmethod
    def _execution(db):
        from .risk import load
        return load(db, "execution_config")

    def _estimated_fee(self, db, cn, order, amount):
        previous = list(db.execute("SELECT notional,fee FROM fills WHERE order_id=?", (order.get("id", ""),)))
        total = sum((number(r[0]) for r in previous), Decimal(0))
        paid = sum((number(r[1]) for r in previous), Decimal(0))
        if cn:
            cumulative = number(cn.charges(db, order["symbol"], total + amount, order["side"])["total"])
        else:
            cumulative = money((total + amount) * number(self._account(db)["fee_bps"]) / 10000)
        return cumulative - paid

    def _risk_record(self, db, payload, *, at=None):
        identifier = uuid.uuid4().hex
        now = at or self._now(self._account(db))
        db.execute("INSERT INTO risk_decisions VALUES(?,?,?)", (identifier, now, canonical(payload)))
        self._event(db, now, "risk_decision", {"id": identifier, "decision": payload})
        return identifier

    def submit_intent(self, intent, observation, *, account_state_sha256=None):
        """Atomic structured batch; models cannot supply policy or execution prices."""
        from .strategy import validate_intent
        from .risk import load, observation_binding
        validation_error = None
        try:
            normalized = validate_intent(intent, observation)
        except (ValueError, KeyError, TypeError) as exc:
            normalized = None
            validation_error = str(exc)
        with self._tx() as db:
            if load(db, "risk_config") is None:
                raise ValueError("submit_intent requires an explicitly configured investment policy")
            now = self._now(self._account(db))
            cn = self._policy(db)
            cn.expire_orders(db, now, self._event)
            if normalized is None:
                payload = {"phase": "intent-validation", "status": "rejected", "errors": [validation_error]}
                rid = self._risk_record(db, payload)
                return {"intent_id": None, "status": "rejected", "orders": [], "reasons": [validation_error], "risk_decision_id": rid}
            identifier = normalized["intent_id"]
            old = db.execute("SELECT * FROM intents WHERE intent_id=?", (identifier,)).fetchone()
            if old:
                if old["payload"] != canonical(normalized) or old["observation"] != canonical(observation):
                    raise ValueError("intent_id reused with different intent/observation")
                return json.loads(old["result"])
            checks, orders, reasons = [], [], []
            try:
                observation_binding(self, db, observation, now, max_quote_age_seconds=normalized["invalidation"]["max_quote_age_seconds"])
            except ValueError as exc:
                reasons.append(str(exc))
            if account_state_sha256 is not None:
                state = {"cash": self._account(db)["cash"],
                         "positions": [dict(r) for r in db.execute("SELECT symbol,qty,cost FROM positions ORDER BY symbol")],
                         "open_orders": [dict(r) for r in db.execute("SELECT * FROM orders WHERE status='open' ORDER BY id")]}
                if digest(state) != account_state_sha256:
                    reasons.append("account economic state changed since frozen observation")
            if timestamp(normalized["as_of"]) > now or timestamp(normalized["invalidation"]["expires_at"]) < now:
                reasons.append("intent is future-dated or expired")
            db.execute("SAVEPOINT intent_batch")
            try:
                if reasons:
                    raise ValueError(reasons[0])
                for index, item in enumerate(normalized["orders"]):
                    observed = observation["series"][item["symbol"]][-1]
                    age_limit = min(self._account(db)["max_age"], normalized["invalidation"]["max_quote_age_seconds"])
                    current = self._quote(db, item["symbol"], now, self._account(db))
                    if age_seconds(now, observed["asof"]) > age_limit or age_seconds(now, current["asof"]) > age_limit:
                        raise ValueError("decision quote exceeds intent freshness limit")
                    order = self._submit(db, ticker=item["symbol"], side=item["side"], qty=item["qty"],
                                         request_id=identifier + ":" + str(index), limit_price=item["limit_price"],
                                         reason=normalized["reason"], intent_id=identifier,
                                         target_weight=item["target_weight"], checks=checks)
                    orders.append(order)
                if not orders:
                    self._event(db, now, "hold", {"reason": normalized["reason"], "intent_id": identifier})
                db.execute("RELEASE intent_batch")
                status = "accepted"
            except ValueError as exc:
                db.execute("ROLLBACK TO intent_batch")
                db.execute("RELEASE intent_batch")
                orders, status = [], "rejected"
                reasons = [str(exc)]
            rid = self._risk_record(db, {"phase": "intent", "intent_id": identifier, "status": status, "checks": checks, "errors": reasons})
            result = {"intent_id": identifier, "status": status, "orders": orders, "reasons": reasons, "risk_decision_id": rid}
            db.execute("INSERT INTO intents VALUES(?,?,?,?)", (identifier, canonical(normalized), canonical(observation), canonical(result)))
            return result

    def _connect(self, readonly=False):
        db = sqlite3.connect(self.path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                             uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _tx(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _account(db):
        return dict(db.execute("SELECT * FROM account WHERE id=1").fetchone())

    @staticmethod
    def _now(account):
        return utcnow() if account["mode"] == "live" else account["clock"]

    @staticmethod
    def _policy(db):
        from .cn import policy
        return policy(db)

    def reconcile(self):
        """Release expired CN DAY orders before account views; never changes fills."""
        with self._tx() as db:
            cn = self._policy(db)
            if cn:
                cn.expire_orders(db, self._now(self._account(db)), self._event)

    def register_instrument(self, data):
        with self._tx() as db:
            cn = self._policy(db)
            if cn is None:
                raise ValueError("instrument registration is for CN accounts only")
            stored = cn.register(db, data)
            self._event(db, self._now(self._account(db)), "instrument", stored)
            return stored

    def register_session(self, data):
        with self._tx() as db:
            cn = self._policy(db)
            if cn is None:
                raise ValueError("daily session registration is for CN accounts only")
            stored = cn.register_day(db, data)
            self._event(db, self._now(self._account(db)), "instrument_day", stored)
            return stored

    @staticmethod
    def _event(db, now, kind, payload):
        prior = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = prior[0] if prior else "0" * 64
        entry = dict(time=now, kind=kind, payload=payload, previous_hash=previous)
        db.execute("INSERT INTO events(time,kind,payload,previous_hash,hash) VALUES(?,?,?,?,?)",
                   (now, kind, canonical(payload), previous, digest(entry)))

    def advance(self, at):
        at = timestamp(at)
        with self._tx() as db:
            a = self._account(db)
            if a["mode"] == "live" or at <= a["clock"]:
                raise ValueError("only demo/replay clock may advance strictly forward")
            cn = self._policy(db)
            if cn:
                cn.day(at, require_open=False)
            db.execute("UPDATE account SET clock=? WHERE id=1", (at,))
            self._event(db, at, "advance", {"clock": at})
            if cn:
                cn.expire_orders(db, at, self._event)
        return {"clock": at}

    def import_quote(self, *, ticker, price, asof, evidence, source="fixture", kind="fixture",
                     currency="USD", received_at=None):
        if kind == "mcp" or source in {"financial-datasets-mcp", "ashare-mcp"}:
            raise ValueError("MCP provenance cannot be asserted by import; use collect-quote")
        return self._store_quote(ticker=ticker, price=price, asof=asof, evidence=evidence, source=source,
                                 kind=kind, currency=currency, received_at=received_at)

    def _store_quote(self, *, ticker, price, asof, evidence, source, kind, currency, received_at=None, _db=None):
        ticker, price, asof = symbol(ticker), number(price, positive=True), timestamp(asof)
        if price != money(price):
            raise ValueError("price precision must be at most 8 decimal places")
        if kind not in {"fixture", "historical", "mcp"} or not isinstance(evidence, dict) or not evidence:
            raise ValueError("quote needs nonempty evidence and known kind")
        if "available_at" in evidence:
            available = timestamp(evidence["available_at"])
            if available < asof:
                raise ValueError("quote cannot be available before its market timestamp")
            evidence = dict(evidence, available_at=available)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source required")
        received = timestamp(received_at or utcnow())
        if kind == "mcp" and (source not in {"financial-datasets-mcp", "ashare-mcp"} or asof > received):
            raise ValueError("MCP quote source invalid or provider time in future")
        if kind == "mcp":
            if source == "financial-datasets-mcp":
                from .market import snapshot
            else:
                from .ashare import snapshot
            parsed = snapshot(evidence, ticker)
            if (parsed["price"], parsed["asof"], parsed["received_at"], parsed["currency"]) != (str(price), asof, received, currency):
                raise ValueError("quote disagrees with MCP evidence")
        entry = dict(symbol=ticker, price=str(price), asof=asof, received_at=received,
                     source=source, kind=kind, currency=currency, evidence=evidence)
        qid = digest(entry)
        with nullcontext(_db) if _db is not None else self._tx() as db:
            a = self._account(db)
            cn = self._policy(db)
            if currency != a["currency"] or (a["mode"] == "live" and kind != "mcp"):
                raise ValueError("account currency/source mode mismatch")
            if cn is not None:
                instrument = cn.instrument(db, ticker)
                if price != cn.round_tick(price, cn.profile(instrument)["tick"], 0):
                    raise ValueError("CN quote violates instrument tick size")
                if kind == "mcp" and source != "ashare-mcp":
                    raise ValueError("CN live quotes must come from the ashare adapter")
            if a["mode"] == "live" and received > utcnow():
                raise ValueError("future collection time")
            cursor = db.execute("INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (qid, ticker, str(price), asof, received, source, kind, currency,
                                canonical(evidence), digest(evidence)))
            if cursor.rowcount:
                self._event(db, self._now(a), "quote", {"quote_id": qid, "entry_hash": qid})
        return {"quote_id": qid}

    def _quote(self, db, ticker, now, a):
        q = db.execute("SELECT * FROM quotes WHERE symbol=? AND asof<=? AND (json_extract(evidence,'$.available_at') IS NULL OR json_extract(evidence,'$.available_at')<=?) ORDER BY asof DESC, received_at DESC, id DESC LIMIT 1",
                       (ticker, now, now)).fetchone()
        if q is None or age_seconds(now, q["asof"]) > a["max_age"]:
            raise ValueError(f"missing/stale quote: {ticker}")
        if a["mode"] == "live" and (q["kind"] != "mcp" or q["received_at"] > now):
            raise ValueError("invalid live quote provenance")
        return dict(q)

    def submit(self, *, ticker, side, qty, request_id, limit_price=None, reason=""):
        from .risk import load
        with self._tx() as db:
            if load(db, "risk_config") is not None:
                raise ValueError("guarded account requires submit_intent; direct orders cannot bypass intent validation")
            return self._submit(db, ticker=ticker, side=side, qty=qty, request_id=request_id, limit_price=limit_price, reason=reason)

    def _submit(self, db, *, ticker, side, qty, request_id, limit_price=None, reason="",
                intent_id=None, target_weight=None, checks=None):
        from .risk import v2, check
        ticker = symbol(ticker)
        if side not in {"buy", "sell"} or type(qty) is not int or not 1 <= qty <= 1000000000:
            raise ValueError("side buy/sell and positive integer qty <= 1e9 required")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("request_id required, max 128 characters")
        if not isinstance(reason, str) or len(reason) > 4000:
            raise ValueError("reason must be text <= 4000 characters")
        limit = None if limit_price is None else str(number(limit_price, positive=True))
        with nullcontext(db):
            a = self._account(db)
            now = self._now(a)
            cn = self._policy(db)
            if cn:
                cn.expire_orders(db, now, self._event)
            old = db.execute("SELECT * FROM orders WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if (old["symbol"], old["side"], old["qty"], old["limit_price"], old["reason"]) != (ticker, side, qty, limit, reason):
                    raise ValueError("request_id reused with different order")
                return dict(old)
            a = self._account(db)
            now = self._now(a)
            cn = self._policy(db)
            if cn is not None:
                cn.expire_orders(db, now, self._event)
                cn.validate_order(db, ticker, side, qty, limit, now)
            q = self._quote(db, ticker, now, a)
            reserved = Decimal(0)
            if side == "buy":
                p = number(limit) if limit else money(number(q["price"]) * (1 + number(a["slippage_bps"]) / 10000))
                cost = money(p * qty)
                reserved = cost + (number(cn.charges(db, ticker, cost, side)["total"]) if cn else money(cost * number(a["fee_bps"]) / 10000))
                others = sum((number(r[0]) for r in db.execute("SELECT reserved_cash FROM orders WHERE status='open'")), Decimal(0))
                if reserved > number(a["cash"]) - others:
                    raise ValueError("insufficient available cash")
            else:
                pos = db.execute("SELECT qty FROM positions WHERE symbol=?", (ticker,)).fetchone()
                from .risk import pending
                used = sum(o["qty"] for o in pending(db) if o["symbol"] == ticker and o["side"] == "sell")
                if qty > (pos[0] if pos else 0) - used:
                    raise ValueError("insufficient available position; short selling disabled")
            order = dict(id=uuid.uuid4().hex, request_id=request_id, symbol=ticker, side=side, qty=qty,
                         limit_price=limit, status="open", created_at=now, reserved_cash=str(reserved), reason=reason)
            if intent_id is not None:
                order["target_weight"] = str(target_weight)
            try:
                decision = check(self, db, order=order, price=number(limit) if limit else number(q["price"]),
                                 qty=qty, fee=self._estimated_fee(db, cn, order, money((number(limit) if limit else number(q["price"])) * qty)), phase="submit", at=now)
            except ValueError as exc:
                decision = {"phase": "submit", "errors": [str(exc)]}
            if decision is not None:
                if checks is not None:
                    checks.append(decision)
                if decision["errors"]:
                    raise ValueError("; ".join(decision["errors"]))
            db.execute("INSERT INTO orders VALUES(:id,:request_id,:symbol,:side,:qty,:limit_price,:status,:created_at,:reserved_cash,:reason)", order)
            if v2(db):
                db.execute("INSERT INTO order_progress VALUES(?,0,?,NULL)", (order["id"], qty))
            if intent_id is not None:
                db.execute("INSERT INTO order_intents VALUES(?,?,?)", (order["id"], intent_id, str(target_weight)))
            self._event(db, now, "submit", order)
            return order

    def cancel(self, order_id):
        with self._tx() as db:
            o = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                raise ValueError("unknown order")
            if o["status"] == "cancelled":
                return dict(o)
            if o["status"] != "open":
                raise ValueError("only open orders may be cancelled")
            cn = self._policy(db)
            if cn:
                cn.validate_cancel(self._now(self._account(db)))
            db.execute("UPDATE orders SET status='cancelled', reserved_cash='0' WHERE id=?", (order_id,))
            self._event(db, self._now(self._account(db)), "cancel", {"order_id": order_id})
            return dict(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())

    def hold(self, reason=""):
        if not isinstance(reason, str) or len(reason) > 4000:
            raise ValueError("reason must be text <= 4000 characters")
        with self._tx() as db:
            self._event(db, self._now(self._account(db)), "hold", {"reason": reason})
        return self.portfolio(record=True)

    def match(self):
        from .risk import v2, remaining, check, load
        results = []
        with self._tx() as db:
            a = self._account(db)
            now = self._now(a)
            cn = self._policy(db)
            modern = v2(db)
            execution = self._execution(db)
            spread = number(execution["spread_bps"]) / 2 if execution else Decimal(0)
            latency = execution["latency_seconds"] if execution else 0
            if cn is not None:
                cn.expire_orders(db, now, self._event)
            for row in db.execute("SELECT * FROM orders WHERE status='open' ORDER BY rowid").fetchall():
                o = dict(row)
                left_before = remaining(db, o)
                protected = load(db, "risk_config")
                stored_intent = None
                if protected:
                    entry = db.execute("SELECT i.payload FROM order_intents o JOIN intents i ON i.intent_id=o.intent_id WHERE o.order_id=?", (o["id"],)).fetchone()
                    stored_intent = json.loads(entry[0]) if entry else None
                    if stored_intent is None or timestamp(stored_intent["invalidation"]["expires_at"]) < now:
                        reason = "validated intent expired or missing"
                        self._risk_record(db, {"phase": "fill", "order_id": o["id"], "errors": [reason]}, at=now)
                        db.execute("UPDATE orders SET status='rejected',reserved_cash='0' WHERE id=?", (o["id"],))
                        self._event(db, now, "reject", {"order_id": o["id"], "reason": reason})
                        results.append({"order_id": o["id"], "status": "rejected", "reason": reason})
                        continue
                try:
                    q = self._quote(db, o["symbol"], now, a)
                except ValueError as exc:
                    results.append({"order_id": o["id"], "status": "open", "reason": str(exc)})
                    continue
                if stored_intent and age_seconds(now, q["asof"]) > stored_intent["invalidation"]["max_quote_age_seconds"]:
                    reason = "quote too old for intent"
                    self._risk_record(db, {"phase": "fill", "order_id": o["id"], "errors": [reason]}, at=now)
                    db.execute("UPDATE orders SET status='rejected',reserved_cash='0' WHERE id=?", (o["id"],))
                    self._event(db, now, "reject", {"order_id": o["id"], "reason": reason})
                    results.append({"order_id": o["id"], "status": "rejected", "reason": reason})
                    continue
                if a["mode"] != "live" and q["asof"] <= o["created_at"]:
                    results.append({"order_id": o["id"], "status": "open", "reason": "waiting for next quote"})
                    continue
                if latency and (age_seconds(now, o["created_at"]) < latency or age_seconds(q["asof"], o["created_at"]) < latency):
                    results.append({"order_id": o["id"], "status": "open", "reason": "waiting for post-latency quote"})
                    continue
                if modern:
                    progress = db.execute("SELECT * FROM order_progress WHERE order_id=?", (o["id"],)).fetchone()
                    if progress["last_quote_asof"] is not None and q["asof"] <= progress["last_quote_asof"]:
                        results.append({"order_id": o["id"], "status": "open", "reason": "new provider timestamp required for another partial fill"})
                        continue
                filled_qty = left_before
                if execution and execution["max_fill_qty"] is not None:
                    consumed = db.execute("SELECT consumed_qty FROM quote_capacity WHERE symbol=? AND asof=?", (o["symbol"], q["asof"])).fetchone()
                    capacity = execution["max_fill_qty"] - (consumed[0] if consumed else 0)
                    filled_qty = min(left_before, capacity)
                    if filled_qty <= 0:
                        results.append({"order_id": o["id"], "status": "open", "reason": "parameterized quote capacity exhausted"})
                        continue
                direction = 1 if o["side"] == "buy" else -1
                effective_slip = number(a["slippage_bps"]) + spread
                price = cn.price(db, o["symbol"], q["price"], effective_slip, o["side"]) if cn else money(number(q["price"]) * (1 + direction * effective_slip / 10000))
                if cn:
                    try:
                        cn.validate_fill(db, dict(o, qty=filled_qty), q, price, now)
                    except ValueError as exc:
                        results.append({"order_id": o["id"], "status": "open", "reason": str(exc)})
                        continue
                if price <= 0:
                    raise ValueError("execution price rounded to zero")
                if o["limit_price"] is not None and direction * (price - number(o["limit_price"])) > 0:
                    results.append({"order_id": o["id"], "status": "open", "reason": "limit not reached after slippage"})
                    continue
                notional = money(price * filled_qty)
                fee = self._estimated_fee(db, cn, o, notional) if modern else (number(cn.charges(db, o["symbol"], notional, o["side"])["total"]) if cn else money(notional * number(a["fee_bps"]) / 10000))
                protected = load(db, "risk_config")
                if protected:
                    link = db.execute("SELECT * FROM order_intents WHERE order_id=?", (o["id"],)).fetchone()
                    rejection = None
                    if link is None:
                        rejection = "guarded order has no validated intent"
                    else:
                        stored = db.execute("SELECT payload FROM intents WHERE intent_id=?", (link["intent_id"],)).fetchone()
                        intent = json.loads(stored[0]) if stored else None
                        if intent is None or timestamp(intent["invalidation"]["expires_at"]) < now:
                            rejection = "validated intent expired or missing"
                        elif age_seconds(now, q["asof"]) > intent["invalidation"]["max_quote_age_seconds"]:
                            rejection = "quote too old for intent"
                        else:
                            o["target_weight"] = link["target_weight"]
                    try:
                        decision = {"phase": "fill", "errors": [rejection]} if rejection else check(self, db, order=o, price=price, qty=filled_qty, fee=fee, phase="fill", at=now)
                    except ValueError as exc:
                        decision = {"phase": "fill", "errors": [str(exc)]}
                    self._risk_record(db, dict(decision, order_id=o["id"]), at=now)
                    if decision["errors"]:
                        db.execute("UPDATE orders SET status='rejected',reserved_cash='0' WHERE id=?", (o["id"],))
                        self._event(db, now, "reject", {"order_id": o["id"], "reason": "; ".join(decision["errors"])})
                        results.append({"order_id": o["id"], "status": "rejected", "reason": "; ".join(decision["errors"])})
                        continue
                cash = number(self._account(db)["cash"])
                pos = db.execute("SELECT * FROM positions WHERE symbol=?", (o["symbol"],)).fetchone()
                qty, cost = (pos["qty"], number(pos["cost"])) if pos else (0, Decimal(0))
                pnl = Decimal(0)
                if o["side"] == "buy":
                    other = sum((number(r[0]) for r in db.execute("SELECT reserved_cash FROM orders WHERE status='open' AND id<>?", (o["id"],))), Decimal(0))
                    remaining_cost = Decimal(0)
                    if modern and left_before > filled_qty:
                        residual_amount = money(number(o["limit_price"] or price) * (left_before-filled_qty))
                        remaining_cost = residual_amount + self._estimated_fee(db, cn, o, notional+residual_amount) - fee
                    if notional + fee + remaining_cost > cash - other:
                        db.execute("UPDATE orders SET status='rejected',reserved_cash='0' WHERE id=?", (o["id"],))
                        self._event(db, now, "reject", {"order_id": o["id"], "reason": "price moved beyond available cash"})
                        results.append({"order_id": o["id"], "status": "rejected"})
                        continue
                    cash -= notional + fee
                    qty += filled_qty
                    cost += notional + fee
                else:
                    if qty < filled_qty:
                        raise ValueError("ledger invariant violated: oversell")
                    other = sum((number(r[0]) for r in db.execute("SELECT reserved_cash FROM orders WHERE status='open' AND id<>?", (o["id"],))), Decimal(0))
                    if cash + notional - fee < other:
                        db.execute("UPDATE orders SET status='rejected',reserved_cash='0' WHERE id=?", (o["id"],))
                        self._event(db, now, "reject", {"order_id": o["id"], "reason": "sale proceeds and available cash cannot cover fees"})
                        results.append({"order_id": o["id"], "status": "rejected", "reason": "insufficient cash to cover sale fees"})
                        continue
                    basis = cost if qty == filled_qty else money(cost * filled_qty / qty)
                    pnl = notional - fee - basis
                    cash += notional - fee
                    qty -= filled_qty
                    cost -= basis
                db.execute("UPDATE account SET cash=? WHERE id=1", (str(cash),))
                db.execute("INSERT INTO positions VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty,cost=excluded.cost", (o["symbol"], qty, str(cost)))
                left_after = left_before - filled_qty
                status = "open" if left_after else "filled"
                if modern:
                    db.execute("UPDATE order_progress SET filled_qty=filled_qty+?,remaining_qty=?,last_quote_asof=? WHERE order_id=?", (filled_qty, left_after, q["asof"], o["id"]))
                    db.execute("INSERT INTO quote_capacity VALUES(?,?,?) ON CONFLICT(symbol,asof) DO UPDATE SET consumed_qty=consumed_qty+excluded.consumed_qty", (o["symbol"], q["asof"], filled_qty))
                db.execute("UPDATE orders SET status=?,reserved_cash='0' WHERE id=?", (status, o["id"]))
                fill = dict(id=uuid.uuid4().hex, order_id=o["id"], quote_id=q["id"], symbol=o["symbol"], side=o["side"],
                            qty=filled_qty, price=str(price), notional=str(notional), fee=str(fee), realized_pnl=str(pnl), time=now)
                db.execute("INSERT INTO fills VALUES(:id,:order_id,:quote_id,:symbol,:side,:qty,:price,:notional,:fee,:realized_pnl,:time)", fill)
                if cn:
                    cn.record_fill(db, fill)
                if left_after and o["side"] == "buy":
                    residual_price = number(o["limit_price"]) if o["limit_price"] is not None else price
                    residual_amount = money(residual_price * left_after)
                    residual_reserve = residual_amount + self._estimated_fee(db, cn, o, residual_amount)
                    if residual_reserve > cash - other:
                        raise ValueError("partial fill violates remaining reservation invariant")
                    db.execute("UPDATE orders SET reserved_cash=? WHERE id=?", (str(residual_reserve), o["id"]))
                self._event(db, now, "fill", fill)
                if modern and load(db, "risk_config") is not None:
                    valuation = self.portfolio(_db=db, _at=now)
                    if valuation["equity"] is not None:
                        db.execute("INSERT INTO risk_state VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET time=excluded.time,equity=excluded.equity", (now, valuation["equity"]))
                        self._event(db, now, "risk_valuation", {"time": now, "equity": valuation["equity"]})
                results.append(fill)
        return results

    def portfolio(self, *, record=False, _db=None, _at=None):
        if _at is not None and (_db is None or record):
            raise ValueError("internal valuation time requires existing transaction and cannot be recorded")
        if _db is None:
            self.reconcile()
        scope = nullcontext(_db) if _db is not None else (self._tx() if record else self._read())
        with scope as db:
            a = self._account(db)
            now = timestamp(_at) if _at is not None else self._now(a)
            cn = self._policy(db)
            positions, missing = [], []
            value, unrealized = Decimal(0), Decimal(0)
            for row in db.execute("SELECT * FROM positions WHERE qty>0 ORDER BY symbol"):
                p = dict(row)
                if cn:
                    sellable = cn.sellable(db, p["symbol"], now)
                    from .risk import pending
                    reserved_qty = sum(o["qty"] for o in pending(db) if o["symbol"] == p["symbol"] and o["side"] == "sell")
                    p.update(sellable_qty=sellable, available_to_sell=max(0, sellable - reserved_qty))
                try:
                    q = self._quote(db, p["symbol"], now, a)
                    mark = money(number(q["price"]) * p["qty"])
                    value += mark
                    unrealized += mark - number(p["cost"])
                    p.update(market_value=str(mark), quote_id=q["id"], quote_asof=q["asof"])
                except ValueError:
                    missing.append(p["symbol"])
                    p.update(market_value=None, quote_id=None)
                positions.append(p)
            reserved = sum((number(r[0]) for r in db.execute("SELECT reserved_cash FROM orders WHERE status='open'")), Decimal(0))
            realized = sum((number(r[0]) for r in db.execute("SELECT realized_pnl FROM fills")), Decimal(0))
            fees = sum((number(r[0]) for r in db.execute("SELECT fee FROM fills")), Decimal(0))
            result = dict(mode=a["mode"], currency=a["currency"], time=now, cash=a["cash"],
                          initial_cash=a["initial_cash"], available_cash=str(number(a["cash"]) - reserved),
                          reserved_cash=str(reserved), positions=positions, missing_marks=missing,
                          equity=None if missing else str(number(a["cash"]) + value),
                          unrealized_pnl=None if missing else str(unrealized), realized_pnl=str(realized), fees=str(fees))
            if cn:
                result.update(market="CN", rules_version=cn.rules["version"], calendar_hash=digest(cn.calendar))
            if record:
                db.execute("INSERT INTO snapshots(time,payload) VALUES(?,?)", (now, canonical(result)))
                self._event(db, now, "snapshot", result)
            return result

    @contextmanager
    def _read(self):
        db = self._connect(readonly=True)
        try:
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    def history(self, table="orders", *, include_progress=False):
        if table not in {"orders", "fills", "events", "snapshots"}:
            raise ValueError("unknown history table")
        with self._read() as db:
            rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            if table == "orders" and include_progress:
                from .risk import remaining
                for order in rows:
                    left = remaining(db, order)
                    order.update(remaining_qty=left, filled_qty=order["qty"]-left)
            return rows
