"""Deterministic local descriptive metrics; no claims of model performance."""

import json
import statistics
from decimal import Decimal

from .common import number, timestamp
from .market import historical


def trade_groups(fills):
    """Whole position episodes from flat to flat, including every partial fill/fee.

    This is explicitly position-episode grouping, not arbitrary sell-fill counting.
    A position never returning to zero remains open and is excluded from win rate.
    """
    active, closed = {}, []
    for fill in fills:
        symbol = fill["symbol"]
        qty = fill["qty"]
        if type(qty) is not int or qty <= 0 or fill["side"] not in {"buy", "sell"}:
            raise ValueError("invalid fill for position grouping")
        if symbol not in active:
            if fill["side"] != "buy":
                raise ValueError("position grouping cannot start with a sale")
            active[symbol] = {"symbol": symbol, "started_at": fill["time"], "qty": 0,
                              "buy_notional": Decimal(0), "sell_notional": Decimal(0),
                              "fees": Decimal(0), "fill_ids": []}
        group = active[symbol]
        group["qty"] += qty if fill["side"] == "buy" else -qty
        if group["qty"] < 0:
            raise ValueError("position grouping detected an oversell")
        group[fill["side"] + "_notional"] += number(fill["notional"])
        group["fees"] += number(fill["fee"])
        group["fill_ids"].append(fill["id"])
        if group["qty"] == 0:
            group["closed_at"] = fill["time"]
            group["net_pnl"] = group["sell_notional"] - group["buy_notional"] - group["fees"]
            closed.append(active.pop(symbol))
    def public(group):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in group.items()}
    return {"method": "per-symbol flat-to-flat position episodes", "closed": [public(g) for g in closed],
            "open": [public(g) for g in active.values()],
            "round_trip_win_rate": None if not closed else sum(g["net_pnl"] > 0 for g in closed) / len(closed)}


def sharpe_for_schedule(points, schedule=None):
    """Compute only with an explicitly declared complete chronological schedule."""
    def unavailable(reason):
        return {"annualized_sharpe": None, "sharpe_null_reason": reason}
    if schedule is None:
        return unavailable("explicit sampling frequency and complete expected schedule required")
    if (not isinstance(schedule, dict) or not {"frequency", "expected_times", "periods_per_year", "min_returns"}.issubset(schedule) or
            set(schedule) - {"frequency", "expected_times", "periods_per_year", "min_returns", "calendar"} or
            schedule["frequency"] not in {"daily", "regular"}):
        raise ValueError("invalid Sharpe sampling configuration")
    if schedule["frequency"] == "regular":
        return unavailable("intraday Sharpe unavailable: verified trading-session sampling grid not implemented")
    expected = [timestamp(t) for t in schedule["expected_times"]]
    if not expected or expected != sorted(set(expected)):
        raise ValueError("Sharpe expected times must be nonempty, sorted and unique")
    periods, minimum = schedule["periods_per_year"], schedule["min_returns"]
    if type(periods) is not int or not 1 <= periods <= 1000000 or type(minimum) is not int or minimum < 20:
        raise ValueError("Sharpe needs positive periods/year and at least 20 returns")
    actual = [timestamp(p["time"]) for p in points]
    if actual != expected:
        return unavailable("observations differ from expected sampling schedule")
    if any(p.get("equity") is None for p in points):
        return unavailable("missing or stale portfolio valuations")
    values = [number(p["equity"], positive=True) for p in points]
    from datetime import datetime
    if schedule["frequency"] == "daily":
        from .cn import SHANGHAI, validate_calendar
        calendar = validate_calendar(schedule.get("calendar"))
        local = [datetime.fromisoformat(t).astimezone(SHANGHAI) for t in expected]
        days = [t.date().isoformat() for t in local]
        if (periods != 252 or len(set(t.time() for t in local)) != 1 or len(set(days)) != len(days) or
                days != [day for day in calendar["trading_days"] if days[0] <= day <= days[-1]]):
            return unavailable("daily sampling must match the complete CN trading calendar and common time")
    returns = [float(b / a - 1) for a, b in zip(values, values[1:])]
    if len(returns) < minimum:
        return unavailable("insufficient complete return samples")
    if schedule["frequency"] == "regular":
        times = [datetime.fromisoformat(t) for t in expected]
        gaps = [(b-a).total_seconds() for a,b in zip(times,times[1:])]
        if len(set(gaps)) != 1:
            return unavailable("declared regular sampling has unequal intervals")
        if 14400 % gaps[0] != 0 or periods != int(252 * 14400 / gaps[0]):
            return unavailable("intraday annualization does not match CN 240-minute session frequency")
    deviation = statistics.stdev(returns)
    if deviation == 0:
        return unavailable("zero return volatility")
    return {"annualized_sharpe": statistics.mean(returns) / deviation * periods ** 0.5,
            "sharpe_null_reason": None, "sharpe_samples": len(returns),
            "sharpe_assumption": "zero risk-free return; declared complete schedule, not statistical adequacy proof"}


def drawdown(values):
    peak, worst = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def market_report(capture, ticker, *, asof):
    data = historical(capture, ticker)
    asof = timestamp(asof)
    rows = [r for r in data["rows"] if r["available_at"] <= asof]
    if not rows:
        raise ValueError("no historical rows available at requested asof")
    closes = [float(number(r["close"])) for r in rows]
    returns = [b / a - 1 for a, b in zip(closes, closes[1:])]
    return {"ticker": ticker, "asof": asof, "rows": len(rows), "start": rows[0]["day"], "end": rows[-1]["day"],
            "pagination_complete": data["pagination_complete"], "capture_sha256": data["capture_sha256"],
            "price_return": closes[-1] / closes[0] - 1, "max_drawdown": drawdown(closes),
            "sma_20": statistics.mean(closes[-20:]) if len(closes) >= 20 else None,
            "sample_return_volatility": statistics.stdev(returns) if len(returns) >= 2 else None,
            "limitations": ["price return excludes dividends and fees", "calendar completeness not verified",
                            "corporate actions and survivorship not verified", "not a clean model benchmark"]}


def performance(trader):
    trader.reconcile()
    with trader._read() as db:
        portfolio = trader.portfolio(_db=db)
        a = trader._account(db)
        snapshots = [json.loads(s["payload"]) for s in db.execute("SELECT * FROM snapshots ORDER BY seq")]
        fills = [dict(f) for f in db.execute("SELECT * FROM fills ORDER BY rowid")]
        quotes = {q["id"]: dict(q) for q in db.execute("SELECT * FROM quotes")}
        started = db.execute("SELECT time FROM events ORDER BY seq LIMIT 1").fetchone()[0]
        cn = trader._policy(db)
        fee_components = None
        if cn:
            rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM fill_charges")]
            fee_components = {key: str(sum((number(r[key]) for r in rows), Decimal(0))) for key in ("commission", "transfer", "stamp_tax")}
    initial = number(portfolio["initial_cash"])
    # Collapse duplicate timestamps, preserve chronological valuation sequence.
    points = {s["time"]: s for s in snapshots}
    points[portfolio["time"]] = portfolio
    ordered = [points[t] for t in sorted(points)]
    valid = [s for s in ordered if s["equity"] is not None]
    missing = len(ordered) - len(valid)
    equities = [float(initial)] + [float(number(s["equity"])) for s in valid]
    sells = [f for f in fills if f["side"] == "sell"]
    slippage = sum(((number(f["price"]) - number(quotes[f["quote_id"]]["price"])) *
                    f["qty"] * (1 if f["side"] == "buy" else -1) for f in fills), Decimal(0))
    groups = trade_groups(fills)
    gross_notional = sum((number(f["notional"]) for f in fills), Decimal(0))
    return {"portfolio": portfolio, "valuation_points": len(valid), "incomplete_valuation_points": missing,
            "fee_bps": a["fee_bps"], "slippage_bps": a["slippage_bps"], "slippage_cost": str(slippage),
            "market": "CN" if cn else "US", "fee_components": fee_components,
            "started_at": started, "asof": portfolio["time"],
            "first_fill_at": fills[0]["time"] if fills else None, "last_fill_at": fills[-1]["time"] if fills else None,
            "net_return": None if portfolio["equity"] is None else str(number(portfolio["equity"]) / initial - 1),
            "observed_max_drawdown": None if missing else drawdown(equities),
            "fills": len(fills), "sell_fills": len(sells),
            "profitable_sell_fill_fraction": (sum(number(f["realized_pnl"]) > 0 for f in sells) / len(sells)) if sells else None,
            "trade_groups": groups, "round_trip_win_rate": groups["round_trip_win_rate"],
            "turnover": str(gross_notional / initial), "turnover_notional": str(gross_notional),
            "turnover_basis": "gross executed buy plus sell notional / initial cash, unannualized",
            "cash_baseline_return": "0", "annualized_sharpe": None,
            "sharpe_null_reason": "report snapshots have no declared complete fixed sampling schedule",
            "limitations": ["drawdown only at recorded observations", "cash baseline has zero interest",
                            "no automatic buy-and-hold benchmark", "no annualization of irregular samples",
                            "fills are simulated, not exchange executions"]}
