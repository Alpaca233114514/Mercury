"""Versioned calculation evidence. The trading ledger remains the economic authority."""
import contextlib
import contextvars
import csv
import hashlib
import io
import json
import pathlib
import sqlite3
import uuid

from .common import canonical, digest, timestamp, utcnow

VERSION = "mercury-records-v1"
SCHEMA = (
    "CREATE TABLE record_meta(id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL, account_id TEXT NOT NULL)",
    "CREATE TABLE record_cycles(cycle_id TEXT PRIMARY KEY, payload TEXT NOT NULL, hash TEXT NOT NULL)",
    "CREATE TABLE record_entries(seq INTEGER PRIMARY KEY AUTOINCREMENT, entry_key TEXT UNIQUE NOT NULL, cycle_id TEXT, kind TEXT NOT NULL, time TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL)",
    "CREATE INDEX record_cycle_lookup ON record_entries(cycle_id,seq)",
    "CREATE TABLE record_intent_links(intent_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL)",
)
_ACTIVE = contextvars.ContextVar("mercury_record_cycle", default=None)


class RecordError(sqlite3.DatabaseError):
    """Recording failures must escape trading validation catch blocks and roll back."""


def enabled(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='record_meta'").fetchone() is None:
        return False
    meta = db.execute("SELECT version FROM record_meta WHERE id=1").fetchone()
    if meta is None or meta[0] != VERSION:
        raise RecordError("unsupported or incomplete records version")
    return True


def initialize(db):
    for sql in SCHEMA:
        db.execute(sql)
    db.execute("INSERT INTO record_meta VALUES(1,?,?)", (VERSION, uuid.uuid4().hex))


def code_identity():
    root = pathlib.Path(__file__).parent
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}
    return {"files": files, "sha256": digest(files)}


def append(db, key, kind, payload, *, time, cycle_id=None):
    if not enabled(db):
        return
    body = {"cycle_id": cycle_id, "kind": kind, "time": time, "payload": payload}
    sha = digest(body)
    old = db.execute("SELECT hash FROM record_entries WHERE entry_key=?", (key,)).fetchone()
    if old:
        if old[0] != sha:
            raise RecordError("immutable calculation record conflict")
        return
    db.execute("INSERT INTO record_entries(entry_key,cycle_id,kind,time,recorded_at,payload,hash) VALUES(?,?,?,?,?,?,?)",
               (key, cycle_id, kind, time, utcnow(), canonical(payload), sha))


def stage(name, value):
    active = _ACTIVE.get()
    if active:
        trader, cycle_id, at = active
        with trader._tx() as db:
            append(db, cycle_id + ":" + name, name, value, time=at, cycle_id=cycle_id)


def bind_intent(db, intent_id):
    active = _ACTIVE.get()
    if active and enabled(db):
        db.execute("INSERT OR IGNORE INTO record_intent_links VALUES(?,?)", (intent_id, active[1]))


@contextlib.contextmanager
def cycle_context(trader, binding, observation, folder):
    cycle_id = digest(binding)
    with trader._tx() as db:
        if not enabled(db):
            raise ValueError("recording requires explicit records upgrade for this legacy account")
        old = db.execute("SELECT payload,hash FROM record_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if old:
            value = json.loads(old[0])
            if digest(value) != old[1] or value["binding"] != binding:
                raise ValueError("cycle record binding mismatch")
        else:
            account = trader._account(db)
            from .risk import load
            cn = trader._policy(db)
            # Names identify experiments; paths and arbitrary agent configuration are not exposed.
            identity = binding["agent"]
            provenance = identity.get("provenance", {})
            value = {"version": VERSION, "cycle_id": cycle_id, "binding": binding,
                     "account_id": db.execute("SELECT account_id FROM record_meta").fetchone()[0],
                     "experiment": pathlib.Path(folder).parent.name,
                     "model": provenance.get("model", identity.get("model")), "decision_maker": provenance.get("decision_maker"),
                     "session_source": provenance.get("session_source"), "adapter": identity.get("provider"),
                     "identity_missing_reason": "undeclared identity fields remain unknown",
                     "market": "CN" if cn else "US", "currency": account["currency"], "mode": account["mode"],
                     "as_of": observation["as_of"], "universe": observation["universe"],
                     "code": code_identity(), "code_role": "recording_started_not_all_stage_execution",
                     "origin": "resumed_existing_stages" if (pathlib.Path(folder)/"intent.json").exists() else "new_cycle",
                     "risk_config": load(db, "risk_config"),
                     "execution_config": load(db, "execution_config"),
                     "market_config_hash": digest(cn.rules) if cn else None,
                     "recorded_at": utcnow()}
            db.execute("INSERT INTO record_cycles VALUES(?,?,?)", (cycle_id, canonical(value), digest(value)))
    token = _ACTIVE.set((trader, cycle_id, observation["as_of"]))
    try:
        stage("observation", observation)
        yield
    except Exception as exc:
        # Never retain exception text or unvalidated model/provider bodies.
        with trader._tx() as db:
            append(db, uuid.uuid4().hex, "failure", {"error_type": type(exc).__name__,
                   "reason": "cycle interrupted or refused"}, time=observation["as_of"], cycle_id=cycle_id)
        raise
    finally:
        _ACTIVE.reset(token)


def failure(phase, exc):
    active = _ACTIVE.get()
    if active:
        trader, cycle_id, at = active
        with trader._tx() as db:
            append(db, uuid.uuid4().hex, "failure", {"phase": phase, "error_type": type(exc).__name__,
                   "reason": "phase refused or unavailable"}, time=at, cycle_id=cycle_id)


def partial_calculation(value):
    active = _ACTIVE.get()
    if active:
        trader, cycle_id, at = active
        with trader._tx() as db:
            append(db, uuid.uuid4().hex, "partial_calculation", value, time=at, cycle_id=cycle_id)


def import_run(trader, path):
    """Import a complete, verified old cycle as history, without inventing its code or traces."""
    from .workflow import read_artifact, checked_audit
    from .observation import validate_observation
    from .strategy import validate_research, validate_strategy, validate_intent
    folder = pathlib.Path(path)
    binding = read_artifact(folder/"manifest.json")
    receipt = read_artifact(folder/"receipt.json")
    cycle_id = digest(binding)
    names = {"account.json", "observation.json", "model-output.json", "intent.json", "risk.json", "previous-review.json"}
    if (binding.get("account_identity") != digest(str(pathlib.Path(trader.path).resolve())) or
            receipt.get("cycle_id") != cycle_id or set(receipt.get("artifact_refs", {})) != names):
        raise ValueError("historical cycle account or receipt binding mismatch")
    values = {name:read_artifact(folder/name) for name in names}
    if any(digest(values[name]) != receipt["artifact_refs"][name] for name in names):
        raise ValueError("historical cycle artifact reference mismatch")
    obs = validate_observation(values["observation.json"])
    decision = values["model-output.json"]
    research = validate_research(decision["research"], obs)
    strategy = validate_strategy(decision["strategy"], obs, research)
    intent = validate_intent(values["intent.json"], obs, strategy)
    if (binding.get("observation_sha256") != digest(obs) or binding.get("observation_id") != obs["observation_id"] or
            binding.get("previous_review_sha256") != digest(values["previous-review.json"]) or
            receipt.get("intent_id") != intent["intent_id"] or receipt.get("risk") != values["risk.json"] or
            trader.intent_result(intent["intent_id"]) != values["risk.json"]):
        raise ValueError("historical cycle differs from source ledger or inputs")
    checked_audit(trader)
    with trader._tx() as db:
        if not enabled(db):
            raise ValueError("records upgrade required before import-run")
        old = db.execute("SELECT payload,hash FROM record_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if old:
            if digest(json.loads(old[0])) != old[1] or json.loads(old[0])["binding"] != binding:
                raise ValueError("historical cycle record conflict")
            return {"cycle_id":cycle_id, "status":"already_recorded"}
        owner = db.execute("SELECT cycle_id FROM record_intent_links WHERE intent_id=?", (intent["intent_id"],)).fetchone()
        if owner and owner[0] != cycle_id:
            raise ValueError("historical intent already attributed to another cycle")
        identity = binding["agent"]
        provenance = identity.get("provenance", {})
        account = trader._account(db)
        value = {"version":VERSION, "cycle_id":cycle_id, "binding":binding, "origin":"imported_history",
                 "account_id":db.execute("SELECT account_id FROM record_meta").fetchone()[0],
                 "experiment":folder.parent.name, "model":provenance.get("model",identity.get("model")),
                 "decision_maker":provenance.get("decision_maker"), "session_source":provenance.get("session_source"),
                 "adapter":identity.get("provider"), "market":"CN", "currency":account["currency"], "mode":account["mode"],
                 "as_of":obs["as_of"], "universe":obs["universe"], "code":None,
                 "missing_reason":"historical execution code and unrecorded computations are unknown",
                 "recorded_at":utcnow()}
        db.execute("INSERT INTO record_cycles VALUES(?,?,?)", (cycle_id,canonical(value),digest(value)))
        db.execute("INSERT INTO record_intent_links VALUES(?,?)", (intent["intent_id"],cycle_id))
        for name, payload in values.items():
            append(db, cycle_id+":"+name[:-5], name[:-5], payload, time=obs["as_of"], cycle_id=cycle_id)
        append(db, cycle_id+":receipt", "receipt", receipt, time=obs["as_of"], cycle_id=cycle_id)
        append(db, cycle_id+":historical-gap", "historical_gap", {"calculation_status":"not_recorded"}, time=obs["as_of"], cycle_id=cycle_id)
    return {"cycle_id":cycle_id, "status":"imported_history", "calculations":"not_recorded"}


def upgrade(trader, backup):
    """SQLite backup under a writer reservation; only additive DDL on the source."""
    backup = pathlib.Path(backup)
    with trader._tx() as db:
        if enabled(db):
            return {"version": VERSION, "status": "already_enabled"}
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        with contextlib.closing(trader._connect(readonly=True)) as source:
            with contextlib.closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("backup integrity check failed")
                if "\n".join(source.iterdump()) != "\n".join(destination.iterdump()):
                    raise ValueError("backup logical content verification failed")
        sha = hashlib.sha256(backup.read_bytes()).hexdigest()
        initialize(db)
        append(db, "upgrade", "upgrade", {"backup_sha256": sha,
               "historical_calculations": "not_recorded"}, time=trader._now(trader._account(db)))
    return {"version": VERSION, "status": "enabled", "backup": str(backup), "backup_sha256": sha}


def _table(db, name):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
        return []
    return [dict(row) for row in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]


def read(trader):
    """One read-only snapshot, never portfolio()/reconcile() and never recompute history."""
    with trader._read() as db:
        enabled(db)
        account = trader._account(db)
        tables = {name: _table(db, name) for name in (
            "record_meta", "record_cycles", "record_entries", "record_intent_links", "orders", "fills", "quotes", "order_intents",
            "order_progress", "fill_charges", "risk_decisions", "intents", "snapshots", "events")}
    source_sha256 = digest(tables)
    cycles = []
    for row in tables["record_cycles"]:
        value = json.loads(row["payload"])
        if digest(value) != row["hash"]:
            raise ValueError("cycle record hash mismatch")
        cycles.append(value)
    entries = []
    for row in tables["record_entries"]:
        row["payload"] = json.loads(row["payload"])
        if digest({k: row[k] for k in ("cycle_id", "kind", "time", "payload")}) != row["hash"]:
            raise ValueError("calculation record hash mismatch")
        entries.append(row)
    by_cycle = {c["cycle_id"]: [] for c in cycles}
    for entry in entries:
        if entry["cycle_id"] in by_cycle:
            by_cycle[entry["cycle_id"]].append(entry)
    intent_cycles = {r["intent_id"]: r["cycle_id"] for r in tables["record_intent_links"]}
    for c in cycles:
        stages = by_cycle[c["cycle_id"]]
        c["entries"] = stages
        intent = next((e["payload"] for e in stages if e["kind"] == "intent"), None)
        c["intent_id"] = intent["intent_id"] if intent else None
        c["intent_owner_cycle_id"] = intent_cycles.get(c["intent_id"])
        receipt = next((e["payload"] for e in stages if e["kind"] == "receipt"), None)
        c["status"] = receipt["execution"]["state"] if receipt else "failed" if any(e["kind"] == "failure" for e in stages) else "incomplete"
    links = {r["order_id"]: r["intent_id"] for r in tables["order_intents"]}
    for entry in entries:
        payload = entry["payload"]
        entry["attributed_cycle_id"] = entry["cycle_id"] or intent_cycles.get(
            links.get(payload.get("order_id"), payload.get("intent_id")))
    progress = {r["order_id"]: r for r in tables["order_progress"]}
    charges = {r["fill_id"]: json.loads(r["payload"]) for r in tables["fill_charges"]}
    fill_evidence = {e["payload"]["fill_id"]: e for e in entries if e["kind"] == "fill_calculation"}
    quotes = {q["id"]: q for q in tables["quotes"]}
    for order in tables["orders"]:
        order.update(intent_id=links.get(order["id"]), cycle_id=intent_cycles.get(links.get(order["id"])))
        order.update(progress.get(order["id"], {}))
    for fill in tables["fills"]:
        fill.update(intent_id=links.get(fill["order_id"]), cycle_id=intent_cycles.get(links.get(fill["order_id"])),
                    charges=charges.get(fill["id"]), calculation=fill_evidence.get(fill["id"]),
                    calculation_status="recorded" if fill["id"] in fill_evidence else "not_recorded",
                    quote=quotes.get(fill["quote_id"]))
        if fill["quote"]:
            fill["quote"] = dict(fill["quote"], evidence=json.loads(fill["quote"]["evidence"]))
    for c in cycles:
        orders = [o for o in tables["orders"] if o["cycle_id"] == c["cycle_id"]]
        fills = [f for f in tables["fills"] if f["cycle_id"] == c["cycle_id"]]
        c.update(orders=orders, fills=fills)
        order_ids = {o["id"] for o in orders}
        c["execution_entries"] = [e for e in entries if e["cycle_id"] != c["cycle_id"] and
                                  (e["payload"].get("order_id") in order_ids or
                                   c["intent_id"] is not None and c["intent_owner_cycle_id"] == c["cycle_id"] and e["payload"].get("intent_id") == c["intent_id"])]
        c["receipt_status"] = c["status"]
        c["workflow_status"] = "completed" if any(e["kind"] == "receipt" for e in c["entries"]) else "failed" if any(e["kind"] == "failure" for e in c["entries"]) else "incomplete"
        if c["intent_owner_cycle_id"] and c["intent_owner_cycle_id"] != c["cycle_id"]:
            c["status"] = "reused_intent"
        if orders:
            if any(o.get("remaining_qty", o["qty"]) > 0 and o["status"] == "open" for o in orders):
                c["status"] = "partially_filled" if fills else "waiting_for_qualified_quote"
            elif all(o["status"] == "filled" for o in orders):
                c["status"] = "filled"
            else:
                c["status"] = "closed_with_remainder" if fills else orders[-1]["status"]
        c["execution_status"] = c["status"]
        if c["workflow_status"] != "completed":
            c["status"] = c["workflow_status"]
    return {"version": VERSION, "account_id": tables["record_meta"][0]["account_id"] if tables["record_meta"] else None,
            "currency": account["currency"], "market": "CN" if account["currency"] == "CNY" else "US", "mode": account["mode"],
            "cutoff": tables["events"][-1]["time"] if tables["events"] else account["clock"],
            "event_head": tables["events"][-1]["hash"] if tables["events"] else None,
            "source_sha256": source_sha256, "cycles": cycles, "entries": entries,
            "orders": tables["orders"], "fills": tables["fills"], "snapshots": tables["snapshots"],
            "limitations": ["local simulated executions", "absent historical calculations were not recorded",
                            "hashes check content consistency, not independent authenticity"]}


def select(data, *, model=None, experiment=None, cycle_id=None, symbol=None, since=None, until=None, status=None):
    since = timestamp(since) if since else None
    until = timestamp(until) if until else None
    if since and until and since > until:
        raise ValueError("since must not exceed until")
    return [c for c in data["cycles"] if
            (model is None or c["model"] == model) and (experiment is None or c["experiment"] == experiment) and
            (cycle_id is None or c["cycle_id"] == cycle_id) and (symbol is None or symbol in c["universe"]) and
            (since is None or c["as_of"] >= since) and (until is None or c["as_of"] <= until) and
            (status is None or c["status"] == status)]


def listing(trader, *, limit=50, offset=0, **filters):
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("limit must be 1..1000 and offset nonnegative")
    data = read(trader)
    cycles = select(data, **filters)
    return {"version": VERSION, "account_id": data["account_id"], "total": len(cycles), "offset": offset,
            "cycles": [{k: c[k] for k in ("cycle_id", "experiment", "model", "as_of", "status", "execution_status", "currency", "universe")}
                       for c in cycles[offset:offset+limit]],
            "unattributed_fills": sum(f["cycle_id"] is None for f in data["fills"])}


def show(trader, *, cycle_id=None, fill_id=None):
    data = read(trader)
    if bool(cycle_id) == bool(fill_id):
        raise ValueError("provide exactly one cycle_id or fill_id")
    if fill_id:
        fill = next((f for f in data["fills"] if f["id"] == fill_id), None)
        if fill is None:
            raise ValueError("unknown fill")
        cycle_id = fill["cycle_id"]
    cycle = next((c for c in data["cycles"] if c["cycle_id"] == cycle_id), None)
    if not fill_id and cycle is None:
        raise ValueError("unknown cycle")
    return {"version": VERSION, "account_id": data["account_id"], "currency": data["currency"],
            "source_sha256": data["source_sha256"], "cycle": cycle,
            "fill": fill if fill_id else None, "attribution": "recorded" if cycle else "unknown"}


def _csv(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        values = {}
        for key in fields:
            value = row.get(key)
            if isinstance(value, (dict, list)):
                value = canonical(value)
            elif value is None:
                value = ""
            else:
                value = str(value)
            if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
                value = "'" + value
            values[key] = value
        writer.writerow(values)
    return stream.getvalue().encode("utf-8-sig")


def export(trader, out, *, format="json", **filters):
    from .workflow import _publish
    data = read(trader)
    selected = select(data, **filters)
    ids = {c["cycle_id"] for c in selected}
    filtered = any(v is not None for v in filters.values())
    data["cycles"] = selected
    for name in ("orders", "fills"):
        if filtered:
            data[name] = [r for r in data[name] if r["cycle_id"] in ids]
    if filtered:
        data["entries"] = [e for e in data["entries"] if e["attributed_cycle_id"] in ids]
        # Global snapshots cannot safely be attributed to a model.
        data["snapshots"] = []
    data["filters"] = filters
    path = pathlib.Path(out)
    if format == "json":
        _publish(path, canonical(data).encode("utf-8"))
        return {"output": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if format != "csv":
        raise ValueError("unsupported export format")
    sets = {
        "cycles": (selected, ["cycle_id", "account_id", "experiment", "model", "as_of", "status", "currency", "entries"]),
        "calculations": ([dict(e, order_id=e["payload"].get("order_id"), fill_id=e["payload"].get("fill_id"),
                               intent_id=e["payload"].get("intent_id")) for e in data["entries"]],
                         ["entry_key", "cycle_id", "attributed_cycle_id", "order_id", "fill_id", "intent_id", "kind", "time", "recorded_at", "payload", "hash"]),
        "orders": (data["orders"], ["id", "cycle_id", "intent_id", "symbol", "side", "qty", "limit_price", "status", "filled_qty", "remaining_qty"]),
        "fills": (data["fills"], ["id", "cycle_id", "intent_id", "order_id", "quote_id", "symbol", "side", "qty", "price", "notional", "fee", "realized_pnl", "time", "charges", "calculation"]),
        "snapshots": (data["snapshots"] + [{"cycle_id": c["cycle_id"], "time": e["time"], "payload": e["payload"]}
                      for c in selected for e in c["entries"] if e["kind"] == "account"], ["seq", "cycle_id", "time", "payload"]),
    }
    blobs = {name + ".csv": _csv(rows, fields) for name, (rows, fields) in sets.items()}
    manifest = {k: data[k] for k in ("version", "account_id", "currency", "market", "mode", "cutoff", "event_head", "source_sha256", "filters", "limitations")}
    manifest["files"] = {name: hashlib.sha256(raw).hexdigest() for name, raw in blobs.items()}
    for name, raw in blobs.items():
        _publish(path/name, raw)
    _publish(path/"manifest.json", canonical(manifest).encode("utf-8"))
    return {"output": str(path), "manifest": manifest}
