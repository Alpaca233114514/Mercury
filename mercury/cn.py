"""CNY-only market policy. Calendar, instrument and daily status are explicit evidence.

No guessed weekdays, exchange suffixes, ST classifications or settlement categories.
Existing USD ledgers never receive a schema migration.
"""

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

from .common import canonical, digest, number, timestamp

SHANGHAI = timezone(timedelta(hours=8))
RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "cn-rules.json"
CN_SCHEMA = """
CREATE TABLE market_config(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE instruments(symbol TEXT PRIMARY KEY, payload TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE instrument_days(symbol TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL, PRIMARY KEY(symbol,day));
CREATE TABLE settlement_lots(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, qty INTEGER NOT NULL, acquired_day TEXT NOT NULL, available_day TEXT NOT NULL, settlement TEXT NOT NULL);
CREATE TABLE fill_charges(fill_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""


def local_dt(at):
    return datetime.fromisoformat(timestamp(at)).astimezone(SHANGHAI)


def cn_symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        raise ValueError("CN symbol requires six digits and explicit .SH/.SZ/.BJ exchange suffix")
    return value


def sourced(data, *, day=None):
    source = data.get("source")
    if not isinstance(source, dict) or source.get("symbol") != data.get("symbol") or not isinstance(source.get("reference"), str) or not source["reference"].strip():
        raise ValueError("source must bind the exact symbol and contain a reference")
    if day is not None and source.get("day") != day:
        raise ValueError("daily source must bind the exact trading date")
    normalized = dict(source)
    normalized["available_at"] = timestamp(source.get("available_at"))
    return normalized


def require_available(data, at):
    if timestamp(data["source"]["available_at"]) > timestamp(at):
        raise ValueError("instrument metadata was not available at the decision/execution time")


def validate_calendar(calendar):
    if not isinstance(calendar, dict) or calendar.get("market") != "CN" or not calendar.get("sources"):
        raise ValueError("CN calendar requires market=CN and official source references")
    start, end = date.fromisoformat(calendar["start"]), date.fromisoformat(calendar["end"])
    days = calendar["trading_days"]
    if start > end or not isinstance(days, list) or not days or days != sorted(set(days)):
        raise ValueError("calendar dates must be nonempty, sorted and unique")
    for day in days:
        parsed = date.fromisoformat(day)
        if not start <= parsed <= end or parsed.weekday() > 4:
            raise ValueError("invalid CN trading date")
    if set(calendar.get("exchanges", [])) != {"SH", "SZ", "BJ"}:
        raise ValueError("calendar must explicitly cover SH, SZ and BJ")
    return json.loads(canonical(calendar))


def create_config(calendar, commission_bps, minimum_commission):
    calendar = validate_calendar(calendar)
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    minimum = number(minimum_commission)
    if not 0 <= minimum <= 100:
        raise ValueError("minimum commission must be 0..100 CNY")
    return {"market": "CN", "currency": "CNY", "calendar": calendar, "rules": rules,
            "commission_bps": str(number(commission_bps)), "minimum_commission": str(minimum),
            "execution": "limit-orders-price-only", "time_in_force": "DAY"}


def policy(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_config'").fetchone():
        return None
    row = db.execute("SELECT payload,hash FROM market_config WHERE id=1").fetchone()
    if row is None:
        raise ValueError("CN market configuration missing")
    config = json.loads(row[0])
    if digest(config) != row[1]:
        raise ValueError("CN market configuration hash mismatch")
    return CNMarket(config)


class CNMarket:
    def __init__(self, config):
        self.config = config
        self.calendar = config["calendar"]
        self.rules = config["rules"]

    def day(self, at, *, require_open=True):
        day = local_dt(at).date().isoformat()
        if not self.calendar["start"] <= day <= self.calendar["end"]:
            raise ValueError("date outside verified CN calendar coverage")
        if require_open and day not in self.calendar["trading_days"]:
            raise ValueError("CN exchange closed on this date")
        if not self.rules["effective_from"] <= day <= self.rules["effective_through"]:
            raise ValueError("date outside configured CN rule validity")
        return day

    def require_session(self, at):
        day = self.day(at)
        t = local_dt(at).time().replace(tzinfo=None)
        if not (time(9, 30) <= t <= time(11, 30) or time(13) <= t < time(14, 57)):
            raise ValueError("outside modeled CN continuous sessions; auctions are unsupported")
        return day

    def validate_cancel(self, at):
        self.day(at)
        t = local_dt(at).time().replace(tzinfo=None)
        if time(9, 20) <= t <= time(9, 25) or time(14, 57) <= t <= time(15):
            raise ValueError("exchange auction cancellation blackout")

    def instrument(self, db, ticker):
        cn_symbol(ticker)
        row = db.execute("SELECT payload,hash FROM instruments WHERE symbol=?", (ticker,)).fetchone()
        if row is None:
            raise ValueError("register sourced instrument classification before trading")
        result = json.loads(row[0])
        if digest(result) != row[1]:
            raise ValueError("instrument metadata hash mismatch")
        return result

    def profile(self, instrument):
        return self.rules["profiles"][instrument["profile"]]

    def register(self, db, data):
        if not isinstance(data, dict) or not data.get("source"):
            raise ValueError("instrument requires source evidence")
        allowed = {"symbol", "profile", "source", "etf_limit_percent"}
        if set(data) - allowed:
            raise ValueError("unexpected instrument fields")
        ticker = cn_symbol(data["symbol"])
        data = dict(data, source=sourced(data))
        if db.execute("SELECT mode FROM account").fetchone()[0] == "live" and data["source"].get("synthetic"):
            raise ValueError("live CN instrument metadata cannot be synthetic")
        p = self.rules["profiles"].get(data.get("profile"))
        if p is None or ticker[-2:] != p["exchange"]:
            raise ValueError("instrument profile/exchange mismatch")
        if p["security_type"] != "stock":
            if str(data.get("etf_limit_percent")) not in p["allowed_limit_percent"]:
                raise ValueError("ETF needs sourced instrument-specific price-limit percentage")
        elif "etf_limit_percent" in data:
            raise ValueError("ETF limit cannot override stock rules")
        db.execute("INSERT INTO instruments VALUES(?,?,?)", (ticker, canonical(data), digest(data)))
        return data

    def register_day(self, db, data):
        required = {"symbol", "day", "reference_price", "suspended", "risk_warning", "listing_session", "source"}
        if not isinstance(data, dict) or set(data) != required or not data["source"]:
            raise ValueError("daily status requires symbol/day/reference_price/suspended/risk_warning/listing_session/source")
        instrument = self.instrument(db, data["symbol"])
        data = dict(data, source=sourced(data, day=data["day"]))
        if db.execute("SELECT mode FROM account").fetchone()[0] == "live" and data["source"].get("synthetic"):
            raise ValueError("live CN daily metadata cannot be synthetic")
        p = self.profile(instrument)
        self.day(data["day"] + "T09:30:00+08:00")
        if type(data["suspended"]) is not bool or type(data["risk_warning"]) is not bool:
            raise ValueError("suspension/risk status must be explicit booleans")
        if type(data["listing_session"]) is not int or data["listing_session"] < 1:
            raise ValueError("listing_session must be a positive verified trading-session ordinal")
        ref = number(data["reference_price"], positive=True)
        if ref != self.round_tick(ref, p["tick"], 0):
            raise ValueError("daily reference price not aligned to tick")
        stored = dict(data, reference_price=str(ref))
        up, down = self.bands(instrument, stored)
        stored.update(limit_up=None if up is None else str(up), limit_down=None if down is None else str(down))
        db.execute("INSERT INTO instrument_days VALUES(?,?,?,?)", (data["symbol"], data["day"], canonical(stored), digest(stored)))
        return stored

    def status(self, db, ticker, at):
        day = self.day(at)
        row = db.execute("SELECT payload,hash FROM instrument_days WHERE symbol=? AND day=?", (ticker, day)).fetchone()
        if row is None:
            raise ValueError("missing sourced daily suspension/limit/listing status")
        result = json.loads(row[0])
        if digest(result) != row[1]:
            raise ValueError("daily instrument status hash mismatch")
        require_available(result, at)
        return result

    def bands(self, instrument, status):
        p = self.profile(instrument)
        if p["security_type"] == "stock" and status["listing_session"] <= p["ipo_unrestricted_sessions"]:
            return None, None
        percent = p["limit_percent"]
        if p["security_type"] != "stock":
            percent = instrument["etf_limit_percent"]
        elif status["risk_warning"]:
            percent = p["risk_limit_percent"]
        reference, rate = number(status["reference_price"]), number(percent) / 100
        return (self.round_tick(reference * (1 + rate), p["tick"], 0),
                self.round_tick(reference * (1 - rate), p["tick"], 0))

    @staticmethod
    def round_tick(value, tick, direction):
        tick = number(tick, positive=True)
        rounding = ROUND_CEILING if direction > 0 else ROUND_FLOOR if direction < 0 else ROUND_HALF_UP
        return (value / tick).quantize(Decimal(1), rounding=rounding) * tick

    def price(self, db, ticker, base, slip_bps, side):
        p = self.profile(self.instrument(db, ticker))
        direction = 1 if side == "buy" else -1
        return self.round_tick(number(base) * (1 + direction * number(slip_bps) / 10000), p["tick"], direction)

    def charges(self, db, ticker, amount, side):
        p = self.profile(self.instrument(db, ticker))
        cents = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        commission = cents(max(number(self.config["minimum_commission"]), amount * number(self.config["commission_bps"]) / 10000))
        transfer = cents(amount * number(p["transfer_bps"]) / 10000)
        stamp = cents(amount * number(p["stamp_sell_bps"]) / 10000) if side == "sell" else Decimal("0.00")
        return {"commission": str(commission), "transfer": str(transfer), "stamp_tax": str(stamp),
                "total": str(commission + transfer + stamp), "currency": "CNY", "rules_version": self.rules["version"]}

    def sellable(self, db, ticker, at):
        day = self.day(at, require_open=False)
        return sum(row["qty"] for row in db.execute("SELECT * FROM settlement_lots WHERE symbol=?", (ticker,))
                   if row["available_day"] <= day)

    def release_day(self, acquired, settlement):
        if settlement == "T0":
            return acquired
        after = [day for day in self.calendar["trading_days"] if day > acquired]
        if not after:
            raise ValueError("next settlement trading day is outside verified calendar")
        return after[0]

    def validate_order(self, db, ticker, side, qty, limit, at):
        self.require_session(at)
        inst = self.instrument(db, ticker)
        require_available(inst, at)
        p, status = self.profile(inst), self.status(db, ticker, at)
        if status["suspended"]:
            raise ValueError("security is suspended")
        if limit is None:
            raise ValueError("CN requires a limit price; unprotected market orders are unsupported")
        price = number(limit, positive=True)
        if self.round_tick(price, p["tick"], 0) != price:
            raise ValueError("order price violates instrument tick size")
        if status["limit_up"] is not None and not number(status["limit_down"]) <= price <= number(status["limit_up"]):
            raise ValueError("order price outside daily price limits")
        if qty > p["max_limit_qty"]:
            raise ValueError("order exceeds instrument quantity maximum")
        if side == "buy":
            if qty < p["min_qty"] or (qty - p["min_qty"]) % p["qty_step"]:
                raise ValueError("buy quantity violates instrument minimum/increment")
            if status["risk_warning"] and p["security_type"] == "stock" and p["exchange"] in {"SH", "SZ"}:
                filled = sum(row["qty"] for row in db.execute("SELECT qty,time FROM fills WHERE symbol=? AND side='buy'", (ticker,)) if local_dt(row["time"]).date().isoformat() == status["day"])
                from .risk import pending
                outstanding = sum(o["qty"] for o in pending(db) if o["symbol"] == ticker and o["side"] == "buy")
                if filled + outstanding + qty > 500000:
                    raise ValueError("risk-warning stock daily buy quota of 500000 exceeded")
        else:
            available = self.sellable(db, ticker, at)
            from .risk import pending
            reserved = sum(o["qty"] for o in pending(db) if o["symbol"] == ticker and o["side"] == "sell")
            if qty > available - reserved:
                raise ValueError("insufficient sellable quantity after T+1 and reservations")
            valid_regular = qty >= p["sell_min_qty"] and (qty - p["sell_min_qty"]) % p["sell_qty_step"] == 0
            if not valid_regular and not (qty == available and reserved == 0):
                raise ValueError("odd-lot sale must dispose of the entire sellable balance")

    def validate_fill(self, db, order, quote, price, at):
        day = self.require_session(at)
        require_available(self.instrument(db, order["symbol"]), at)
        status = self.status(db, order["symbol"], at)
        if status["suspended"]:
            raise ValueError("security is suspended")
        if local_dt(quote["asof"]).date().isoformat() != day:
            raise ValueError("CN fill cannot reuse a quote from another trading date")
        self.require_session(quote["asof"])
        if status["limit_up"] is not None:
            lo, hi, observed = number(status["limit_down"]), number(status["limit_up"]), number(quote["price"])
            if not lo <= observed <= hi or not lo <= price <= hi:
                raise ValueError("quote/execution outside daily price limits")
            if (order["side"] == "buy" and observed >= hi) or (order["side"] == "sell" and observed <= lo):
                raise ValueError("limit-locked direction cannot fill without order-book liquidity evidence")
        if order["side"] == "sell" and order["qty"] > self.sellable(db, order["symbol"], at):
            raise ValueError("T+1 sellable quantity insufficient at execution")

    def record_fill(self, db, fill):
        day = self.day(fill["time"])
        if fill["side"] == "buy":
            p = self.profile(self.instrument(db, fill["symbol"]))
            release = self.release_day(day, p["settlement"])
            db.execute("INSERT INTO settlement_lots VALUES(?,?,?,?,?,?)", (fill["id"], fill["symbol"], fill["qty"], day, release, p["settlement"]))
        else:
            remaining = fill["qty"]
            for lot in db.execute("SELECT * FROM settlement_lots WHERE symbol=? AND qty>0 ORDER BY acquired_day,rowid", (fill["symbol"],)).fetchall():
                if lot["available_day"] <= day:
                    used = min(remaining, lot["qty"])
                    db.execute("UPDATE settlement_lots SET qty=qty-? WHERE id=?", (used, lot["id"]))
                    remaining -= used
                    if remaining == 0:
                        break
            if remaining:
                raise ValueError("settlement lot invariant violated")
        from .risk import v2
        amount = number(fill["notional"])
        prior = []
        if v2(db):
            amount = sum((number(r[0]) for r in db.execute("SELECT notional FROM fills WHERE order_id=?", (fill["order_id"],))), Decimal(0))
            prior = [json.loads(r[0]) for r in db.execute("SELECT c.payload FROM fill_charges c JOIN fills f ON f.id=c.fill_id WHERE f.order_id=?", (fill["order_id"],))]
        charges = self.charges(db, fill["symbol"], amount, fill["side"])
        for key in ("commission", "transfer", "stamp_tax", "total"):
            charges[key] = str(number(charges[key]) - sum((number(p[key]) for p in prior), Decimal(0)))
        if number(charges["total"]) != number(fill["fee"]):
            raise ValueError("incremental fee accounting invariant violated")
        db.execute("INSERT INTO fill_charges VALUES(?,?)", (fill["id"], canonical(charges)))

    def expire_orders(self, db, at, event):
        day = local_dt(at).date().isoformat()
        after_close = local_dt(at).time().replace(tzinfo=None) > time(15)
        for o in db.execute("SELECT * FROM orders WHERE status='open'").fetchall():
            created = local_dt(o["created_at"]).date().isoformat()
            if created < day or (created == day and after_close):
                db.execute("UPDATE orders SET status='expired',reserved_cash='0' WHERE id=?", (o["id"],))
                event(db, at, "expire", {"order_id": o["id"], "reason": "CN DAY order expired"})
