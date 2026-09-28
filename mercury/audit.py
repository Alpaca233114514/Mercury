"""Independent ledger reconstruction from stored fills, without engine mutation."""

import json
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR

from .common import age_seconds, digest, timestamp


def _charges(cn, profile, account, amount, side):
    D = Decimal
    if not cn:
        return {"total": (amount * D(account["fee_bps"]) / 10000).quantize(D("0.00000001"), rounding=ROUND_HALF_UP)}
    cents = lambda x: x.quantize(D("0.01"), rounding=ROUND_HALF_UP)
    commission = cents(max(D(cn.config["minimum_commission"]), amount * D(account["fee_bps"]) / 10000))
    transfer = cents(amount * D(profile["transfer_bps"]) / 10000)
    stamp = cents(amount * D(profile["stamp_sell_bps"]) / 10000) if side == "sell" else D("0.00")
    return dict(commission=commission, transfer=transfer, stamp_tax=stamp, total=commission+transfer+stamp)


def audit(trader):
    errors = []
    D = Decimal
    quant = lambda x: x.quantize(D("0.00000001"), rounding=ROUND_HALF_UP)
    with trader._read() as db:
        a = dict(db.execute("SELECT * FROM account").fetchone())
        cn = trader._policy(db)
        from .risk import v2, load
        modern = v2(db)
        execution = load(db, "execution_config") if modern else None
        cash, holdings = D(a["initial_cash"]), {}
        previous = "0" * 64
        events = [dict(e) for e in db.execute("SELECT * FROM events ORDER BY seq")]
        if not events or events[0]["kind"] != "init":
            errors.append("missing initialization event")
        else:
            init = json.loads(events[0]["payload"])
            if modern and init.get("execution_policy") != execution:
                errors.append("execution policy differs from initialization")
            if cn is not None and init.get("market_config") != cn.config:
                errors.append("CN market configuration differs from initialization event")
            for key, val in {"cash": a["initial_cash"], "fee_bps": a["fee_bps"], "slippage_bps": a["slippage_bps"],
                             "mode": a["mode"], "currency": a["currency"], "max_age": a["max_age"]}.items():
                if init.get(key) != val:
                    errors.append(f"account configuration differs from init: {key}")
        fill_events = {}
        order_events = {}
        terminal = {}
        quote_events = set()
        for e in events:
            payload = json.loads(e["payload"])
            entry = dict(time=e["time"], kind=e["kind"], payload=payload, previous_hash=previous)
            if e["previous_hash"] != previous or e["hash"] != digest(entry):
                errors.append(f"event chain mismatch at {e['seq']}")
            previous = e["hash"]
            if e["kind"] == "fill":
                fill_events[payload["id"]] = payload
                if not modern:
                    terminal[payload["order_id"]] = "filled"
            elif e["kind"] == "submit":
                order_events[payload["id"]] = payload
            elif e["kind"] in {"cancel", "reject", "expire"}:
                terminal[payload["order_id"]] = {"cancel": "cancelled", "reject": "rejected", "expire": "expired"}[e["kind"]]
            elif e["kind"] == "quote":
                quote_events.add(payload["quote_id"])
        quotes = {q["id"]: dict(q) for q in db.execute("SELECT * FROM quotes")}
        if set(quotes) != quote_events:
            errors.append("quote/event membership mismatch")
        for q in quotes.values():
            evidence = json.loads(q["evidence"])
            entry = {k: q[k] for k in ("symbol", "price", "asof", "received_at", "source", "kind", "currency")}
            entry["evidence"] = evidence
            if digest(evidence) != q["evidence_hash"] or digest(entry) != q["id"]:
                errors.append(f"quote/evidence hash mismatch: {q['id']}")
            if q["kind"] == "mcp":
                if q["source"] == "ashare-mcp":
                    from .ashare import snapshot
                else:
                    from .market import snapshot
                try:
                    parsed = snapshot(evidence, q["symbol"])
                    if (parsed["price"], parsed["asof"], parsed["received_at"], parsed["currency"]) != (q["price"], q["asof"], q["received_at"], q["currency"]):
                        errors.append(f"quote differs from MCP envelope: {q['id']}")
                except (ValueError, KeyError, TypeError):
                    errors.append(f"invalid MCP envelope: {q['id']}")
        orders = {o["id"]: dict(o) for o in db.execute("SELECT * FROM orders")}
        progress = {}
        totals = {}
        for f in db.execute("SELECT * FROM fills ORDER BY rowid"):
            total = totals.setdefault(f["order_id"], {"qty": 0, "amount": D(0), "fee": D(0), "asof": None})
            total["qty"] += f["qty"]
            total["amount"] += D(f["notional"])
            total["fee"] += D(f["fee"])
            total["asof"] = quotes[f["quote_id"]]["asof"]
        if modern:
            progress = {r["order_id"]: dict(r) for r in db.execute("SELECT * FROM order_progress")}
            if set(progress) != set(orders):
                errors.append("progress/order membership mismatch")
        if set(orders) != set(order_events):
            errors.append("order/event membership mismatch")
        for oid, o in orders.items():
            submitted = order_events.get(oid, {})
            for k in ("symbol", "side", "qty", "limit_price", "request_id", "reason", "created_at"):
                if submitted.get(k) != o[k]:
                    errors.append(f"order changed: {oid}/{k}")
            total = totals.get(oid, {"qty": 0, "amount": D(0), "fee": D(0), "asof": None})
            left = o["qty"] - total["qty"]
            if modern:
                expected_progress = dict(order_id=oid, filled_qty=total["qty"], remaining_qty=left, last_quote_asof=total["asof"])
                if progress.get(oid) != expected_progress or left < 0:
                    errors.append(f"order progress differs from fills: {oid}")
            if o["status"] != terminal.get(oid, "filled" if modern and left == 0 else "open"):
                errors.append(f"order status inconsistent: {oid}")
            expected_reserve = submitted.get("reserved_cash") if o["status"] == "open" else "0"
            if modern and total["qty"] and o["status"] == "open":
                if o["side"] == "buy":
                    profile = cn.profile(cn.instrument(db, o["symbol"])) if cn else None
                    last_fill = db.execute("SELECT price FROM fills WHERE order_id=? ORDER BY rowid DESC LIMIT 1", (oid,)).fetchone()
                    residual = quant(D(o["limit_price"] or last_fill[0]) * left)
                    expected_reserve = str(residual + _charges(cn, profile, a, total["amount"]+residual, o["side"])["total"] - total["fee"])
                else:
                    expected_reserve = "0"
            if D(o["reserved_cash"]) != D(expected_reserve):
                errors.append(f"order reservation inconsistent: {oid}")
        fills = [dict(f) for f in db.execute("SELECT * FROM fills ORDER BY rowid")]
        if {f["id"] for f in fills} != set(fill_events):
            errors.append("fill/event membership mismatch")
        accumulated, last_asof, capacity = {}, {}, {}
        for f in fills:
            if f != fill_events.get(f["id"]):
                errors.append(f"fill/event mismatch: {f['id']}")
            q, o = quotes.get(f["quote_id"]), orders.get(f["order_id"])
            if q is None or o is None:
                errors.append(f"missing quote/order: {f['id']}")
                continue
            if (f["symbol"], f["side"]) != (o["symbol"], o["side"]) or not 0 < f["qty"] <= o["qty"] or (not modern and f["qty"] != o["qty"]) or q["symbol"] != f["symbol"]:
                errors.append(f"fill identity mismatch: {f['id']}")
            if f["time"] < o["created_at"] or not 0 <= age_seconds(f["time"], q["asof"]) <= a["max_age"]:
                errors.append(f"stale/future fill: {f['id']}")
            quote_available = json.loads(q["evidence"]).get("available_at", q["asof"])
            if timestamp(quote_available) > f["time"]:
                errors.append(f"fill used unavailable quote: {f['id']}")
            if a["mode"] != "live" and q["asof"] <= o["created_at"]:
                errors.append(f"same-bar historical fill: {f['id']}")
            if a["mode"] == "live" and (q["kind"] != "mcp" or q["received_at"] > f["time"]):
                errors.append(f"invalid live provenance: {f['id']}")
            direction = 1 if f["side"] == "buy" else -1
            slip = D(a["slippage_bps"]) + (D(execution["spread_bps"])/2 if execution else 0)
            price = quant(D(q["price"]) * (1 + direction * slip / 10000))
            if execution:
                latency = execution["latency_seconds"]
                if latency and (age_seconds(q["asof"], o["created_at"]) < latency or age_seconds(f["time"], o["created_at"]) < latency):
                    errors.append(f"execution latency violated: {f['id']}")
                if o["id"] in last_asof and q["asof"] <= last_asof[o["id"]]:
                    errors.append(f"partial reused provider timestamp: {f['id']}")
                last_asof[o["id"]] = q["asof"]
                key = (f["symbol"], q["asof"])
                capacity[key] = capacity.get(key, 0) + f["qty"]
                if execution["max_fill_qty"] is not None and capacity[key] > execution["max_fill_qty"]:
                    errors.append(f"configured quote capacity exceeded: {f['id']}")
            if cn:
                profile = cn.profile(cn.instrument(db, f["symbol"]))
                tick = D(profile["tick"])
                price = ((D(q["price"]) * (1 + direction * slip / 10000)) / tick).quantize(D(1), rounding=ROUND_CEILING if direction > 0 else ROUND_FLOOR) * tick
            amount = quant(price * f["qty"])
            fee = quant(amount * D(a["fee_bps"]) / 10000)
            if modern and not cn:
                previous_amount, previous_fee = accumulated.get(o["id"], (D(0), D(0)))
                cumulative_fee = quant((previous_amount+amount) * D(a["fee_bps"]) / 10000)
                fee = cumulative_fee-previous_fee
                accumulated[o["id"]] = (previous_amount+amount, cumulative_fee)
            if cn:
                previous_amount, previous_charges = accumulated.get(o["id"], (D(0), {"commission": D(0), "transfer": D(0), "stamp_tax": D(0), "total": D(0)}))
                cumulative = _charges(cn, profile, a, previous_amount + amount if modern else amount, f["side"])
                increment = {k: value - previous_charges[k] if modern else value for k, value in cumulative.items()}
                accumulated[o["id"]] = (previous_amount + amount, cumulative)
                commission, transfer, stamp = increment["commission"], increment["transfer"], increment["stamp_tax"]
                fee = commission + transfer + stamp
                charges = db.execute("SELECT payload FROM fill_charges WHERE fill_id=?", (f["id"],)).fetchone()
                expected = dict(commission=str(commission), transfer=str(transfer), stamp_tax=str(stamp), total=str(fee), currency="CNY", rules_version=cn.rules["version"])
                if charges is None or json.loads(charges[0]) != expected:
                    errors.append(f"CN fee components differ from independent arithmetic: {f['id']}")
            if (D(f["price"]), D(f["notional"]), D(f["fee"])) != (price, amount, fee):
                errors.append(f"execution arithmetic mismatch: {f['id']}")
            if o["limit_price"] is not None and direction * (price - D(o["limit_price"])) > 0:
                errors.append(f"limit violated: {f['id']}")
            qty, cost = holdings.get(f["symbol"], (0, D(0)))
            pnl = D(0)
            if f["side"] == "buy":
                cash -= amount + fee
                holdings[f["symbol"]] = (qty + f["qty"], cost + amount + fee)
            else:
                if f["qty"] > qty or qty <= 0:
                    errors.append(f"oversell: {f['id']}")
                    continue
                basis = cost if qty == f["qty"] else quant(cost * f["qty"] / qty)
                pnl = amount - fee - basis
                cash += amount - fee
                holdings[f["symbol"]] = (qty - f["qty"], cost - basis)
            if D(f["realized_pnl"]) != pnl:
                errors.append(f"realized PnL mismatch: {f['id']}")
            if cash < 0:
                errors.append(f"negative cash after fill: {f['id']}")
        stored = {p["symbol"]: (p["qty"], D(p["cost"])) for p in db.execute("SELECT * FROM positions")}
        if stored != holdings:
            errors.append("positions/cost differ from replayed fills")
        if cash != D(a["cash"]):
            errors.append("cash differs from replayed fills")
        reserved = sum((D(o["reserved_cash"]) for o in orders.values() if o["status"] == "open"), D(0))
        if not 0 <= reserved <= cash:
            errors.append("cash reservations exceed balance")
        sell_reserves = {}
        for o in orders.values():
            if o["status"] == "open" and o["side"] == "sell":
                sell_reserves[o["symbol"]] = sell_reserves.get(o["symbol"], 0) + (o["qty"] - totals.get(o["id"], {"qty": 0})["qty"])
        for ticker, qty in sell_reserves.items():
            if qty > holdings.get(ticker, (0,))[0]:
                errors.append(f"sell reservations exceed holding: {ticker}")
        snaps = [json.loads(s["payload"]) for s in db.execute("SELECT * FROM snapshots ORDER BY seq")]
        if snaps != [json.loads(e["payload"]) for e in events if e["kind"] == "snapshot"]:
            errors.append("snapshot/event mismatch")
        if modern:
            actual_capacity = {(r["symbol"],r["asof"]):r["consumed_qty"] for r in db.execute("SELECT * FROM quote_capacity")}
            if actual_capacity != capacity:
                errors.append("quote capacity differs from fills")
            errors.extend(_audit_risk(db, events, orders, fills))
            errors.extend(_replay_risk(db, events, quotes, a, cn))
        errors.extend(_check_observations(events, quotes, a, cn))
        if cn:
            from .cn_audit import audit_cn
            errors.extend(audit_cn(db, cn, events, quotes))
    return {"ok": not errors, "errors": errors, "fills_checked": len(fills), "events_checked": len(events),
            "reconstructed_cash": str(cash), "event_head": previous,
            "limitations": ["local hash consistency, not externally authenticated evidence",
                            "not an independent agent review or proof of real-world execution"]}


def _check_observations(events, quotes, account, cn=None):
    """Replay cash, inventory and known quotes at every recorded valuation event."""
    D = Decimal
    quant = lambda x: x.quantize(D("0.00000001"), rounding=ROUND_HALF_UP)
    cash, pnl, fees = D(account["initial_cash"]), D(0), D(0)
    positions, known, open_orders, errors = {}, {}, {}, []
    cumulative, profiles = {}, {}
    for event in events:
        p = json.loads(event["payload"])
        kind = event["kind"]
        if kind == "quote":
            q = quotes.get(p["quote_id"])
            if q:
                known[q["id"]] = q
        elif kind == "submit":
            open_orders[p["id"]] = p
        elif kind in {"cancel", "reject", "expire"}:
            open_orders.pop(p["order_id"], None)
        elif kind == "instrument":
            profiles[p["symbol"]] = cn.rules["profiles"][p["profile"]]
        elif kind == "fill":
            if p["quote_id"] not in known or p["order_id"] not in open_orders:
                errors.append(f"fill preceded quote/order: {p['id']}")
            old_amount, old_fee = cumulative.get(p["order_id"], (D(0), D(0)))
            amount_total, fee_total = old_amount+D(p["notional"]), old_fee+D(p["fee"])
            cumulative[p["order_id"]] = (amount_total, fee_total)
            order = open_orders.get(p["order_id"])
            if order:
                order["qty"] -= p["qty"]
                if not order["qty"]:
                    open_orders.pop(p["order_id"], None)
                elif order["side"] == "buy":
                    residual = quant(D(order["limit_price"] or p["price"]) * order["qty"])
                    order["reserved_cash"] = str(residual+_charges(cn, profiles.get(order["symbol"]), account, amount_total+residual, order["side"])["total"]-fee_total)
            quantity, basis = positions.get(p["symbol"], (0, D(0)))
            amount, fee = D(p["notional"]), D(p["fee"])
            fees += fee
            if p["side"] == "buy":
                cash -= amount + fee
                positions[p["symbol"]] = (quantity + p["qty"], basis + amount + fee)
            elif quantity >= p["qty"] and quantity > 0:
                disposed = basis if quantity == p["qty"] else quant(basis * p["qty"] / quantity)
                cash += amount - fee
                pnl += amount - fee - disposed
                positions[p["symbol"]] = (quantity - p["qty"], basis - disposed)
        elif kind == "snapshot":
            market_value, unrealized, missing, expected_positions = D(0), D(0), [], []
            for ticker, (quantity, basis) in sorted(positions.items()):
                if quantity <= 0:
                    continue
                candidates = [q for q in known.values() if q["symbol"] == ticker and q["asof"] <= p["time"] and json.loads(q["evidence"]).get("available_at", q["asof"]) <= p["time"]]
                latest = max(candidates, key=lambda q: (q["asof"], q["received_at"], q["id"])) if candidates else None
                item = {"symbol": ticker, "qty": quantity, "cost": str(basis)}
                if latest is None or age_seconds(p["time"], latest["asof"]) > account["max_age"]:
                    missing.append(ticker)
                    item.update(market_value=None, quote_id=None)
                else:
                    value = quant(D(latest["price"]) * quantity)
                    market_value += value
                    unrealized += value - basis
                    item.update(market_value=str(value), quote_id=latest["id"], quote_asof=latest["asof"])
                expected_positions.append(item)
            reserve = sum((D(o["reserved_cash"]) for o in open_orders.values()), D(0))
            expected = {"cash": cash, "available_cash": cash - reserve, "reserved_cash": reserve,
                        "realized_pnl": pnl, "fees": fees, "equity": None if missing else cash + market_value,
                        "unrealized_pnl": None if missing else unrealized}
            for key, value in expected.items():
                actual = None if p.get(key) is None else D(p[key])
                if actual != value:
                    errors.append(f"snapshot {event['seq']} {key} differs from reconstruction")
            actual_positions = [{k: v for k, v in row.items() if k not in {"sellable_qty", "available_to_sell"}} for row in p.get("positions", [])]
            if p.get("missing_marks") != missing or actual_positions != expected_positions:
                errors.append(f"snapshot {event['seq']} positions/marks differ from reconstruction")
    return errors


def _audit_risk(db, events, orders, fills):
    from .risk import load
    from .strategy import validate_intent
    errors = []
    policy = load(db, "risk_config")
    policies = [json.loads(e["payload"]) for e in events if e["kind"] == "risk_policy"]
    if policies != ([policy] if policy else []):
        errors.append("risk policy differs from immutable event")
    decisions = {r["id"]: json.loads(r["payload"]) for r in db.execute("SELECT * FROM risk_decisions")}
    evidence = {json.loads(e["payload"])["id"]: json.loads(e["payload"])["decision"] for e in events if e["kind"] == "risk_decision"}
    if decisions != evidence:
        errors.append("risk decisions differ from event evidence")
    intents = {r["intent_id"]: dict(r) for r in db.execute("SELECT * FROM intents")}
    links = {r["order_id"]: dict(r) for r in db.execute("SELECT * FROM order_intents")}
    if policy and set(links) != set(orders):
        errors.append("guarded order lacks intent link")
    for identifier, row in intents.items():
        intent, observation, receipt = (json.loads(row[k]) for k in ("payload", "observation", "result"))
        try:
            if validate_intent(intent, observation) != intent or intent["intent_id"] != identifier:
                errors.append("intent schema/content hash mismatch")
        except (ValueError, KeyError, TypeError):
            errors.append("stored intent invalid")
        decision = decisions.get(receipt.get("risk_decision_id"), {})
        if decision.get("status") != receipt.get("status") or decision.get("intent_id") != identifier:
            errors.append("intent receipt differs from risk decision")
        expected = [o for o in orders.values() if o["id"] in links and links[o["id"]]["intent_id"] == identifier]
        if receipt.get("status") == "rejected" and expected:
            errors.append("rejected intent committed orders")
        if receipt.get("status") == "accepted" and len(expected) != len(intent["orders"]):
            errors.append("intent batch/order count mismatch")
        for index, item in enumerate(intent["orders"]):
            matches = [o for o in expected if o["request_id"] == identifier+":"+str(index)]
            if receipt.get("status") == "accepted" and (len(matches) != 1 or any(matches[0][k] != item[k] for k in ("symbol", "side", "qty", "limit_price"))):
                errors.append("intent order differs from submitted order")
    for decision in decisions.values():
        checks = decision.get("checks", [decision] if decision.get("phase") == "fill" else [])
        for check in checks:
            if check.get("errors") or not policy:
                continue
            if check.get("policy_hash") != digest(policy):
                errors.append("risk check references different policy")
                continue
            D = Decimal
            if D(check["notional"])+D(check["fee"]) > D(policy["max_order_notional"]):
                errors.append("accepted risk check exceeds single order budget")
            baseline = D(check["baseline_equity"])
            if baseline <= 0:
                if check["order"]["side"] == "buy":
                    errors.append("accepted buy lacks daily baseline")
                baseline = D(check["equity"])
            if D(check["turnover"])+D(check["committed_turnover"]) > baseline*D(policy["max_daily_turnover"]):
                errors.append("accepted risk check exceeds daily turnover")
            if check["order"]["side"] == "buy":
                projected = D(check["projected_equity"])
                if any(D(v) > projected*D(policy["max_position_weight"]) for v in check["exposure"].values()):
                    errors.append("accepted risk check exceeds concentration")
                if projected < baseline*(1-D(policy["max_daily_loss"])):
                    errors.append("accepted buy exceeds daily loss")
    if policy:
        checked_fills = [d for d in decisions.values() if d.get("phase") == "fill" and not d.get("errors")]
        for fill in fills:
            if not any(d.get("order_id") == fill["order_id"] and d.get("time") == fill["time"] and d.get("qty") == fill["qty"] and Decimal(d.get("price", "-1")) == Decimal(fill["price"]) for d in checked_fills):
                errors.append("guarded fill lacks matching risk recheck")
    return errors


def _replay_risk(db, events, quotes, account, cn):
    """Recompute investment-check budgets from event-time cash/orders/quotes."""
    import copy
    from .cn import local_dt
    from .risk import load
    policy = load(db, "risk_config")
    if policy is None:
        return []
    D = Decimal
    quant = lambda x: x.quantize(D("0.00000001"), rounding=ROUND_HALF_UP)
    cash, holdings, pending, known, paid, turnover = D(account["initial_cash"]), {}, {}, {}, {}, {}
    submitted_states, errors, last_valuation, bases, recorded_snapshots = {}, [], None, {}, []

    def state():
        return copy.deepcopy((cash, holdings, pending, known, paid, turnover, last_valuation, recorded_snapshots))

    def verify(check, replay):
        bal, inv, opens, available, executed, traded, prior_state, prior_snapshots = replay
        at = check["time"]
        marks = {}
        for ticker in set(inv) | {o["symbol"] for o in opens.values()} | {check["order"]["symbol"]}:
            candidates = [q for q in available.values() if q["symbol"] == ticker and q["asof"] <= at and timestamp(json.loads(q["evidence"]).get("available_at", q["asof"])) <= at]
            if not candidates:
                errors.append("accepted risk check lacks replay valuation")
                return
            q = max(candidates, key=lambda q: (q["asof"], q["received_at"], q["id"]))
            if age_seconds(at, q["asof"]) > account["max_age"]:
                errors.append("accepted risk check uses stale replay valuation")
                return
            marks[ticker] = D(q["price"])
        equity = bal + sum((quant(marks[t]*qty) for t,qty in inv.items()), D(0))
        day=local_dt(at).date().isoformat()
        if day not in bases:
            candidates=[v for v in prior_snapshots if v.get("equity") is not None and local_dt(v["time"]).date().isoformat() < day]
            if prior_state and local_dt(prior_state["time"]).date().isoformat() < day:
                candidates.append(prior_state)
            previous=max(candidates,key=lambda v:v["time"]) if candidates else None
            if previous:
                base,basis=D(previous["equity"]),"prior recorded complete valuation"
            elif not inv:
                base,basis=equity,"known cash-only daily baseline"
            else:
                base,basis=D(0),"baseline_missing"
            bases[day]=(str(base),basis)
        base,basis=bases[day]
        if D(check["baseline_equity"]) != D(base) or check["baseline_basis"] != basis:
            errors.append("daily baseline differs from independent prior valuation replay")
        if check.get("errors"):
            return
        exposure = {t: quant(marks[t]*qty) for t,qty in inv.items() if qty > 0}
        candidate = dict(check["order"],qty=check["qty"],limit_price=check["price"])
        commitments = [dict(o) for key,o in opens.items() if key != candidate["id"]]
        if check["phase"] == "fill":
            original = opens.get(candidate["id"])
            if original is None:
                errors.append("risk fill recheck preceded its order")
                return
            left = original["qty"]-candidate["qty"]
            if left:
                commitments.append(dict(original,qty=left))
        commitments.append(candidate)
        grouped, fees, adverse, commitment_total = {}, D(0), D(0), D(0)
        for item in commitments:
            worst = max(D(item["limit_price"]),marks[item["symbol"]])
            amount = quant(worst*item["qty"])
            old_amount, old_fee = grouped.get(item["id"], (D(0),D(0)))
            filled_amount, filled_fee = executed.get(item["id"], (D(0),D(0)))
            profile = cn.profile(cn.instrument(db,item["symbol"]))
            cumulative = _charges(cn,profile,account,filled_amount+old_amount+amount,item["side"])["total"]-filled_fee
            increment = cumulative-old_fee
            grouped[item["id"]]=(old_amount+amount,cumulative)
            commitment_total += amount+increment
            if item["side"] == "buy":
                exposure[item["symbol"]]=exposure.get(item["symbol"],D(0))+amount
                fees += increment
                adverse += max(D(0),worst-marks[item["symbol"]])*item["qty"]
        projected=equity-fees-adverse
        day=local_dt(at).date().isoformat()
        expected={"equity":equity,"projected_equity":projected,"turnover":traded.get(day,D(0)),"committed_turnover":commitment_total}
        prior_amount,prior_fee=executed.get(candidate["id"],(D(0),D(0)))
        notional=quant(D(check["price"])*check["qty"])
        profile=cn.profile(cn.instrument(db,candidate["symbol"]))
        candidate_fee=_charges(cn,profile,account,prior_amount+notional,candidate["side"])["total"]-prior_fee
        expected.update(notional=notional,fee=candidate_fee)
        if any(D(check[k]) != value for k,value in expected.items()) or {k:D(v) for k,v in check["exposure"].items()} != exposure:
            errors.append("risk budget differs from independent event replay")

    for event in events:
        p=json.loads(event["payload"])
        kind=event["kind"]
        if kind == "quote":
            known[p["quote_id"]]=quotes[p["quote_id"]]
        elif kind == "submit":
            pending[p["id"]]=dict(p)
            submitted_states[p["id"]]=state()
        elif kind in {"cancel","reject","expire"}:
            pending.pop(p["order_id"],None)
        elif kind == "fill":
            quantity=p["qty"] if p["side"] == "buy" else -p["qty"]
            holdings[p["symbol"]]=holdings.get(p["symbol"],0)+quantity
            if not holdings[p["symbol"]]:
                holdings.pop(p["symbol"])
            amount,fee=D(p["notional"]),D(p["fee"])
            cash += (-amount-fee) if p["side"] == "buy" else (amount-fee)
            old_amount,old_fee=paid.get(p["order_id"],(D(0),D(0)))
            paid[p["order_id"]]=(old_amount+amount,old_fee+fee)
            day=local_dt(p["time"]).date().isoformat()
            turnover[day]=turnover.get(day,D(0))+amount+fee
            if p["order_id"] in pending:
                pending[p["order_id"]]["qty"]-=p["qty"]
                if not pending[p["order_id"]]["qty"]:
                    pending.pop(p["order_id"])
        elif kind == "risk_decision":
            d=p["decision"]
            if d.get("phase") == "intent" and d.get("status") == "accepted":
                for check in d["checks"]:
                    if not check.get("errors"):
                        verify(check,submitted_states[check["order"]["id"]])
                        last_valuation={"time":check["time"],"equity":check["equity"]}
            elif d.get("phase") == "fill":
                if "baseline_equity" in d:
                    verify(d,state())
                if d.get("equity") is not None:
                    last_valuation={"time":d["time"],"equity":d["equity"]}
        elif kind == "risk_valuation":
            market=D(0)
            for ticker,qty in holdings.items():
                candidates=[q for q in known.values() if q["symbol"] == ticker and q["asof"] <= p["time"] and timestamp(json.loads(q["evidence"]).get("available_at",q["asof"])) <= p["time"]]
                if not candidates:
                    errors.append("risk valuation lacks recorded mark")
                    continue
                q=max(candidates,key=lambda q:(q["asof"],q["received_at"],q["id"]))
                market+=quant(D(q["price"])*qty)
            if D(p["equity"]) != cash+market:
                errors.append("risk post-fill valuation differs from replay")
            last_valuation=p
        elif kind == "snapshot" and p.get("equity") is not None:
            recorded_snapshots.append(p)
    stored_bases={r["day"]:(r["baseline_equity"],r["basis"]) for r in db.execute("SELECT * FROM risk_days")}
    if any(stored_bases.get(day) != base for day,base in bases.items()):
        errors.append("risk daily baseline differs from committed decision evidence")
    actual=db.execute("SELECT time,equity FROM risk_state WHERE id=1").fetchone()
    if (dict(actual) if actual else None) != last_valuation:
        errors.append("last risk valuation differs from decision/fill evidence")
    return errors
