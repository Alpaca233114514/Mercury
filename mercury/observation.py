"""Public, account-independent CN observations; never expose capture archives.

The runner owns raw archives and database access. This module provides a bounded
JSON projection, not an OS sandbox or an authentication claim about source hashes.
"""

import json
import re
from datetime import date

from .common import canonical, digest, number, timestamp
from .cn import cn_symbol, local_dt

VERSION = "cn-observation-v1"
SHA = re.compile(r"[0-9a-f]{64}")
LOT_FIELDS = {"min_qty", "qty_step", "sell_min_qty", "sell_qty_step", "max_limit_qty", "tick"}
PATH = re.compile(r"(?:[A-Za-z]:[\\/]|file://|/(?:Users|home|tmp|var|mnt)/|\\\\[^\s]+\\)")


def exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(label + " has missing or unknown fields")


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("expected lowercase SHA-256 identifier")
    return value


def text(value, *, limit=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or PATH.search(value):
        raise ValueError("text must be nonempty, bounded and contain no internal file path")
    return value


def decimal(value, *, positive=False):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("decimal values must be strings")
    parsed = number(value, positive=positive)
    if parsed and parsed.adjusted() < -18:
        raise ValueError("decimal scale exceeds supported bounded precision")
    return parsed


def _time(value):
    normalized = timestamp(value)
    if normalized != value:
        raise ValueError("public timestamps must be canonical UTC ISO-8601")
    return normalized


def _seal(body, key):
    return dict(body, **{key: digest(body)})


def _identity(value, key):
    sha(value[key])
    if digest({k: v for k, v in value.items() if k != key}) != value[key]:
        raise ValueError(key + " does not match content")


def observation_references(obs):
    refs = {r["quote_id"] for rows in obs["series"].values() for r in rows}
    refs.update(v["source_id"] for v in obs["instruments"].values())
    refs.update(v["source_id"] for v in obs["status"].values())
    refs.update(v["document_id"] for v in obs["documents"])
    return refs


def validate_observation(obs):
    exact(obs, {"version", "observation_id", "as_of", "market", "currency", "universe", "lookback",
                "calendar", "rules", "series", "instruments", "status", "documents", "missing", "limitations"}, "observation")
    canonical(obs)  # Reject non-finite JSON values anywhere before projection.
    if obs["version"] != VERSION or obs["market"] != "CN" or obs["currency"] != "CNY":
        raise ValueError("only versioned CN/CNY observations supported")
    cutoff = _time(obs["as_of"])
    universe = obs["universe"]
    if (not isinstance(universe, list) or not 1 <= len(universe) <= 1000 or
            any(not isinstance(s, str) for s in universe) or universe != sorted(set(universe))):
        raise ValueError("universe must be sorted, unique and bounded")
    for ticker in universe:
        cn_symbol(ticker)
    if type(obs["lookback"]) is not int or not 1 <= obs["lookback"] <= 10000:
        raise ValueError("lookback must be 1..10000 rows per symbol")
    exact(obs["calendar"], {"version", "sha256", "day", "is_open", "trading_days"}, "calendar")
    cal = obs["calendar"]
    sha(cal["sha256"])
    text(cal["version"], limit=100)
    day = local_dt(cutoff).date().isoformat()
    if cal["day"] != day or type(cal["is_open"]) is not bool:
        raise ValueError("calendar cutoff day invalid")
    days = cal["trading_days"]
    if not isinstance(days, list) or any(not isinstance(d, str) for d in days) or days != sorted(set(days)):
        raise ValueError("calendar trading days must be sorted and unique")
    for d in days:
        date.fromisoformat(d)
        if d > day:
            raise ValueError("observation calendar contains future trading days")
    if cal["is_open"] != (day in days):
        raise ValueError("calendar open flag mismatch")
    exact(obs["rules"], {"version", "sha256", "effective_from", "effective_through"}, "rules")
    sha(obs["rules"]["sha256"])
    text(obs["rules"]["version"], limit=100)
    date.fromisoformat(obs["rules"]["effective_from"])
    date.fromisoformat(obs["rules"]["effective_through"])
    if not obs["rules"]["effective_from"] <= day <= obs["rules"]["effective_through"]:
        raise ValueError("cutoff outside rules validity")
    if not isinstance(obs["series"], dict) or set(obs["series"]) != set(universe):
        raise ValueError("series must exactly match universe")
    for ticker, rows in obs["series"].items():
        if not isinstance(rows, list) or len(rows) > obs["lookback"]:
            raise ValueError("series exceeds lookback")
        seen, previous = set(), None
        for row in rows:
            exact(row, {"quote_id", "asof", "available_at", "price", "kind", "frequency", "source_hash"}, "quote row")
            sha(row["quote_id"])
            sha(row["source_hash"])
            at, available = _time(row["asof"]), _time(row["available_at"])
            if at > cutoff or available > cutoff or available < at or row["kind"] not in {"mcp", "historical", "fixture"}:
                raise ValueError("quote future/availability/kind invalid")
            if previous is not None and at <= previous or at in seen:
                raise ValueError("series times must strictly increase")
            previous = at
            seen.add(at)
            decimal(row["price"], positive=True)
            if row["frequency"] not in {"day", "snapshot", "fixture", "unknown", "1min", "5min", "15min", "30min", "60min"}:
                raise ValueError("unsupported quote frequency")
    for field in ("instruments", "status"):
        if not isinstance(obs[field], dict) or not set(obs[field]).issubset(universe):
            raise ValueError("metadata outside observation universe")
        for ticker, row in obs[field].items():
            fields = {"profile", "available_at", "source_id", "source_hash", "lot"} if field == "instruments" else {
                "day", "available_at", "source_id", "source_hash", "suspended", "risk_warning",
                "listing_session", "reference_price", "limit_up", "limit_down"}
            exact(row, fields, field)
            sha(row["source_id"])
            sha(row["source_hash"])
            if _time(row["available_at"]) > cutoff:
                raise ValueError("future metadata in observation")
            if field == "instruments":
                text(row["profile"], limit=100)
                exact(row["lot"], LOT_FIELDS, "lot")
                for key in LOT_FIELDS - {"tick"}:
                    if type(row["lot"][key]) is not int or row["lot"][key] <= 0:
                        raise ValueError("lot quantities must be positive integers")
                decimal(row["lot"]["tick"], positive=True)
            else:
                if row["day"] != day or type(row["suspended"]) is not bool or type(row["risk_warning"]) is not bool:
                    raise ValueError("security daily status identity invalid")
                if type(row["listing_session"]) is not int or row["listing_session"] <= 0:
                    raise ValueError("listing session must be verified positive integer")
                decimal(row["reference_price"], positive=True)
                for key in ("limit_up", "limit_down"):
                    if row[key] is not None:
                        decimal(row[key], positive=True)
    if not isinstance(obs["documents"], list) or len(obs["documents"]) > 1000:
        raise ValueError("documents must be a bounded list")
    seen_documents = set()
    for doc in obs["documents"]:
        exact(doc, {"document_id", "kind", "symbol", "published_at", "available_at", "title", "summary", "source_hash"}, "document")
        _validate_document(doc, universe, cutoff)
        _identity(doc, "document_id")
        if doc["document_id"] in seen_documents:
            raise ValueError("duplicate document")
        seen_documents.add(doc["document_id"])
    if not isinstance(obs["missing"], list):
        raise ValueError("missing must be a list")
    for gap in obs["missing"]:
        exact(gap, {"symbol", "field", "reason"}, "missing item")
        if gap["symbol"] not in universe or gap["field"] not in {"series", "instrument", "status"}:
            raise ValueError("invalid missing-data descriptor")
        text(gap["reason"], limit=200)
    if not isinstance(obs["limitations"], list):
        raise ValueError("limitations must be a list")
    for limitation in obs["limitations"]:
        text(limitation, limit=300)
    _identity(obs, "observation_id")
    return json.loads(canonical(obs))


def _validate_document(doc, universe, cutoff):
    if not isinstance(doc["kind"], str) or doc["kind"] not in {"news", "announcement", "fundamental"} or doc["symbol"] not in universe:
        raise ValueError("document kind/symbol invalid")
    pub, available = _time(doc["published_at"]), _time(doc["available_at"])
    if pub > cutoff or available > cutoff or available < pub:
        raise ValueError("document publication/availability exceeds cutoff")
    text(doc["title"], limit=512)
    text(doc["summary"], limit=8000)
    sha(doc["source_hash"])


def build_observation(trader, universe, *, documents=(), lookback=60, as_of=None):
    if (not isinstance(universe, (list, tuple)) or any(not isinstance(s, str) for s in universe) or
            not universe or len(universe) > 1000 or len(set(universe)) != len(universe)):
        raise ValueError("explicit unique bounded universe required")
    universe = sorted(cn_symbol(s) for s in universe)
    if type(lookback) is not int or not 1 <= lookback <= 10000:
        raise ValueError("lookback must be 1..10000")
    if not isinstance(documents, (list, tuple)) or len(documents) > 1000:
        raise ValueError("documents must be bounded JSON descriptors")
    series, instruments, status, gaps = {s: [] for s in universe}, {}, {}, []
    with trader._read() as db:
        account = trader._account(db)
        cn = trader._policy(db)
        if account["currency"] != "CNY" or cn is None:
            raise ValueError("Observation requires a CN/CNY data account")
        now = timestamp(trader._now(account))
        cutoff = timestamp(as_of) if as_of else now
        if cutoff > now:
            raise ValueError("observation cutoff cannot exceed account clock")
        day = cn.day(cutoff, require_open=False)
        for ticker in universe:
            quotes = db.execute("SELECT * FROM quotes WHERE symbol=? AND asof<=? ORDER BY asof,id", (ticker, cutoff)).fetchall()
            by_time = {}
            for q in quotes:
                evidence = json.loads(q["evidence"])
                if digest(evidence) != q["evidence_hash"]:
                    raise ValueError("quote evidence hash mismatch")
                entry = {k: q[k] for k in ("symbol", "price", "asof", "received_at", "source", "kind", "currency")}
                if digest(dict(entry, evidence=evidence)) != q["id"]:
                    raise ValueError("quote content ID mismatch")
                available = timestamp(evidence.get("available_at", q["asof"]))
                if available > cutoff or (account["mode"] == "live" and q["received_at"] > cutoff):
                    continue
                if q["currency"] != "CNY":
                    raise ValueError("wrong-currency quote in CN observation")
                row = {"quote_id": q["id"], "asof": timestamp(q["asof"]), "available_at": available,
                       "price": str(number(q["price"], positive=True)), "kind": q["kind"], "source_hash": q["evidence_hash"],
                       "frequency": evidence.get("frequency", "snapshot" if q["kind"] == "mcp" else "fixture" if q["kind"] == "fixture" else "unknown")}
                # Multiple captures at one reported time are resolved by stable ID,
                # never by a collection timestamp that may be later than replay cutoff.
                previous = by_time.get(row["asof"])
                if previous is not None and (previous["price"], previous["frequency"]) != (row["price"], row["frequency"]):
                    raise ValueError("conflicting prices/frequencies at the same provider time")
                if previous is None or row["quote_id"] < previous["quote_id"]:
                    by_time[row["asof"]] = row
            series[ticker] = [by_time[k] for k in sorted(by_time)][-lookback:]
            try:
                inst = cn.instrument(db, ticker)
                if timestamp(inst["source"]["available_at"]) <= cutoff:
                    profile = cn.profile(inst)
                    instruments[ticker] = {"profile": inst["profile"], "available_at": timestamp(inst["source"]["available_at"]),
                        "source_id": digest(inst["source"]), "source_hash": digest(inst),
                        "lot": {key: profile[key] for key in LOT_FIELDS}}
            except ValueError as exc:
                if str(exc) != "register sourced instrument classification before trading":
                    raise
            row = db.execute("SELECT payload,hash FROM instrument_days WHERE symbol=? AND day=?", (ticker, day)).fetchone()
            if row:
                meta = json.loads(row["payload"])
                if digest(meta) != row["hash"]:
                    raise ValueError("security status hash mismatch")
                if timestamp(meta["source"]["available_at"]) <= cutoff:
                    status[ticker] = {k: meta[k] for k in ("day", "suspended", "risk_warning", "listing_session", "reference_price", "limit_up", "limit_down")}
                    status[ticker].update(available_at=timestamp(meta["source"]["available_at"]),
                                          source_id=digest(meta["source"]), source_hash=digest(meta))
            for field, present in (("series", bool(series[ticker])), ("instrument", ticker in instruments), ("status", ticker in status)):
                if not present:
                    gaps.append({"symbol": ticker, "field": field, "reason": "no verified data available by cutoff"})
        calendar = {"version": cn.calendar.get("version", "cn-calendar-content-v1"), "sha256": digest(cn.calendar),
                    "day": day, "is_open": day in cn.calendar["trading_days"],
                    "trading_days": [d for d in cn.calendar["trading_days"] if d <= day]}
        rules = {k: cn.rules[k] for k in ("version", "effective_from", "effective_through")}
        rules["sha256"] = digest(cn.rules)
    selected_docs = []
    for doc in documents:
        exact(doc, {"kind", "symbol", "published_at", "available_at", "title", "summary", "source_hash"}, "document input")
        # Normalize timestamps before filtering, but never copy future text into output.
        candidate = dict(doc, published_at=timestamp(doc["published_at"]), available_at=timestamp(doc["available_at"]))
        if candidate["published_at"] > cutoff or candidate["available_at"] > cutoff:
            continue
        _validate_document(candidate, universe, cutoff)
        selected_docs.append(_seal(candidate, "document_id"))
    selected_docs.sort(key=lambda d: (d["published_at"], d["document_id"]))
    body = {"version": VERSION, "as_of": cutoff, "market": "CN", "currency": "CNY", "universe": universe,
            "lookback": lookback, "calendar": calendar, "rules": rules, "series": series,
            "instruments": instruments, "status": status, "documents": selected_docs, "missing": gaps,
            "limitations": ["raw captures and document archives are excluded from public observations",
                            "lookback and signal windows count unique observations, not trading days",
                            "document coverage and source authenticity are not guaranteed",
                            "calendar and rule publication times are not independently verified",
                            "application projection is not OS isolation from a privileged host"]}
    return validate_observation(_seal(body, "observation_id"))


freeze_observation = build_observation
