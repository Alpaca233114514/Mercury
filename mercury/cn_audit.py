"""Reconstruct CN settlement lots and metadata use from event order, read-only."""

import json
from decimal import Decimal
from .common import digest, timestamp
from .cn import local_dt, sourced
from datetime import time


def audit_cn(db, cn, events, quotes):
    errors, instruments, statuses, lots, pending = [], {}, {}, {}, {}
    bought = {}

    def eligible(ticker, day):
        return sum(l["qty"] for l in lots.values() if l["symbol"] == ticker and l["available_day"] <= day)

    for e in events:
        p, kind = json.loads(e["payload"]), e["kind"]
        day = local_dt(e["time"]).date().isoformat()
        if kind == "instrument":
            instruments[p["symbol"]] = p
            try:
                sourced(p)
            except ValueError:
                errors.append("CN instrument source identity/time invalid")
        elif kind == "instrument_day":
            statuses[(p["symbol"], p["day"])] = p
            try:
                sourced(p, day=p["day"])
            except ValueError:
                errors.append("CN daily source identity/time invalid")
        elif kind == "submit":
            inst = instruments.get(p["symbol"])
            state = statuses.get((p["symbol"], day))
            if inst is None or state is None:
                errors.append(f"CN submit preceded instrument/daily evidence: {p['id']}")
                continue
            profile = cn.rules["profiles"][inst["profile"]]
            if any(timestamp(item["source"]["available_at"]) > e["time"] for item in (inst, state)):
                errors.append(f"CN order used future metadata: {p['id']}")
            try:
                cn.require_session(e["time"])
            except ValueError:
                errors.append(f"CN order outside trading session: {p['id']}")
            if state["suspended"] or p["limit_price"] is None:
                errors.append(f"CN invalid suspension/limit order: {p['id']}")
            if p["qty"] > profile["max_limit_qty"]:
                errors.append(f"CN order quantity exceeds maximum: {p['id']}")
            regular = p["qty"] >= profile["min_qty"] and (p["qty"] - profile["min_qty"]) % profile["qty_step"] == 0
            if p["side"] == "sell":
                regular = p["qty"] >= profile["sell_min_qty"] and (p["qty"] - profile["sell_min_qty"]) % profile["sell_qty_step"] == 0
                available = eligible(p["symbol"], day)
                reserved = sum(o["qty"] for o in pending.values() if o["symbol"] == p["symbol"] and o["side"] == "sell")
                if p["qty"] > available - reserved:
                    errors.append(f"CN order violates T+1/reserved sellable quantity: {p['id']}")
                if not regular and not (p["qty"] == available and reserved == 0):
                    errors.append(f"CN odd-lot disposal violation: {p['id']}")
            elif not regular:
                errors.append(f"CN buy quantity rule violation: {p['id']}")
            if p["side"] == "buy" and state["risk_warning"] and profile["security_type"] == "stock" and profile["exchange"] in {"SH", "SZ"}:
                outstanding = sum(o["qty"] for o in pending.values() if o["symbol"] == p["symbol"] and o["side"] == "buy")
                if bought.get((p["symbol"], day), 0) + outstanding + p["qty"] > 500000:
                    errors.append(f"CN risk-warning daily buy quota exceeded: {p['id']}")
            if p["limit_price"] is not None:
                value, tick = Decimal(p["limit_price"]), Decimal(profile["tick"])
                if value % tick or (state["limit_up"] is not None and not Decimal(state["limit_down"]) <= value <= Decimal(state["limit_up"])):
                    errors.append(f"CN order tick/price-band violation: {p['id']}")
            pending[p["id"]] = p
        elif kind in {"cancel", "reject", "expire"}:
            o = pending.pop(p["order_id"], None)
            if kind == "cancel":
                try:
                    cn.validate_cancel(e["time"])
                except ValueError:
                    errors.append(f"CN cancellation blackout violated: {p['order_id']}")
            if kind == "expire" and o:
                created = local_dt(o["created_at"]).date().isoformat()
                if not (created < day or (created == day and local_dt(e["time"]).time().replace(tzinfo=None) > time(15))):
                    errors.append("CN order expired before DAY validity ended")
        elif kind == "fill":
            o = pending.get(p["order_id"])
            inst = instruments.get(p["symbol"])
            state = statuses.get((p["symbol"], day))
            if o is None or inst is None or state is None:
                errors.append(f"CN fill missing prior order/metadata: {p['id']}")
                continue
            original = dict(o)
            o["qty"] -= p["qty"]
            if o["qty"] == 0:
                pending.pop(p["order_id"], None)
            elif o["qty"] < 0:
                errors.append(f"CN partial exceeded order quantity: {p['id']}")
            o = original
            if local_dt(o["created_at"]).date().isoformat() != day:
                errors.append(f"CN DAY order filled on another date: {p['id']}")
            profile = cn.rules["profiles"][inst["profile"]]
            if any(timestamp(item["source"]["available_at"]) > e["time"] for item in (inst, state)):
                errors.append(f"CN fill used future metadata: {p['id']}")
            q = quotes.get(p["quote_id"])
            if not q:
                continue
            try:
                cn.require_session(e["time"])
                cn.require_session(q["asof"])
            except ValueError:
                errors.append(f"CN fill outside supported session: {p['id']}")
            if local_dt(q["asof"]).date().isoformat() != day or state["suspended"]:
                errors.append(f"CN suspended/previous-day quote fill: {p['id']}")
            if state["limit_up"] is not None:
                lo, hi = Decimal(state["limit_down"]), Decimal(state["limit_up"])
                observed, executed = Decimal(q["price"]), Decimal(p["price"])
                if not lo <= observed <= hi or not lo <= executed <= hi:
                    errors.append(f"CN execution outside price band: {p['id']}")
                if (p["side"] == "buy" and observed >= hi) or (p["side"] == "sell" and observed <= lo):
                    errors.append(f"CN locked-limit liquidity assumed: {p['id']}")
            if p["side"] == "buy":
                bought[(p["symbol"], day)] = bought.get((p["symbol"], day), 0) + p["qty"]
                release = day if profile["settlement"] == "T0" else next((d for d in cn.calendar["trading_days"] if d > day), None)
                lots[p["id"]] = dict(id=p["id"], symbol=p["symbol"], qty=p["qty"], acquired_day=day, available_day=release, settlement=profile["settlement"])
            else:
                left = p["qty"]
                for lot in lots.values():
                    if lot["symbol"] == p["symbol"] and lot["available_day"] <= day:
                        take = min(left, lot["qty"])
                        lot["qty"] -= take
                        left -= take
                        if not left:
                            break
                if left:
                    errors.append(f"CN settlement oversell: {p['id']}")
        elif kind == "snapshot":
            if p.get("market") != "CN" or p.get("calendar_hash") != digest(cn.calendar) or p.get("rules_version") != cn.rules["version"]:
                errors.append(f"CN snapshot configuration mismatch: {e['seq']}")
            for position in p["positions"]:
                amount = eligible(position["symbol"], day)
                reserved = sum(o["qty"] for o in pending.values() if o["symbol"] == position["symbol"] and o["side"] == "sell")
                if position.get("sellable_qty") != amount or position.get("available_to_sell") != max(0, amount - reserved):
                    errors.append(f"CN snapshot sellable quantity mismatch: {e['seq']}")
    actual_lots = {r["id"]: dict(r) for r in db.execute("SELECT * FROM settlement_lots")}
    if actual_lots != lots:
        errors.append("CN settlement lots differ from replayed fills")
    for table, expected, keys in [("instruments", instruments, ("symbol",)), ("instrument_days", statuses, ("symbol", "day"))]:
        actual = {}
        for row in db.execute(f"SELECT * FROM {table}"):
            content = json.loads(row["payload"])
            key = row[keys[0]] if len(keys) == 1 else tuple(row[k] for k in keys)
            actual[key] = content
            if digest(content) != row["hash"]:
                errors.append(f"CN {table} hash mismatch")
        if actual != expected:
            errors.append(f"CN {table} differs from registration events")
    return errors
