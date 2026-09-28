import copy
import json
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path

from mercury.cn import CNMarket, CN_SCHEMA
from mercury.common import canonical, digest, timestamp
from mercury.engine import SCHEMA
from mercury.observation import build_observation, validate_observation

ROOT = Path(__file__).resolve().parent.parent
TICKER = "600519.SH"
AT = timestamp("2026-09-24T10:00:00+08:00")


class MemoryTrader:
    """Only in-memory controlled source data; no user ledger or filesystem writes."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA + CN_SCHEMA)
        rules = json.loads((ROOT / "config/cn-rules.json").read_text(encoding="utf-8"))
        calendar = json.loads((ROOT / "config/cn-calendar-2026.json").read_text(encoding="utf-8"))
        self.cn = CNMarket({"rules": rules, "calendar": calendar})
        self.account = {"clock": AT, "currency": "CNY", "mode": "replay"}
        source = {"symbol": TICKER, "reference": "C:/private/internal/secret.json",
                  "available_at": timestamp("2026-09-24T09:00:00+08:00")}
        inst = {"symbol": TICKER, "profile": "SH_MAIN_STOCK", "source": source}
        state = {"symbol": TICKER, "day": "2026-09-24", "suspended": False, "risk_warning": False,
                 "listing_session": 100, "reference_price": "10", "limit_up": "11", "limit_down": "9",
                 "source": dict(source, day="2026-09-24")}
        self.db.execute("INSERT INTO instruments VALUES(?,?,?)", (TICKER, canonical(inst), digest(inst)))
        self.db.execute("INSERT INTO instrument_days VALUES(?,?,?,?)", (TICKER, state["day"], canonical(state), digest(state)))
        for minute, price in (("58", "10"), ("59", "10.1"), ("00", "10.2")):
            at = f"2026-09-24T{'09' if minute != '00' else '10'}:{minute}:00+08:00"
            self.quote(price, at)
        self.db.commit()

    def quote(self, price, at, **evidence):
        at = timestamp(at)
        raw = {"synthetic": True, "capture": {"future": [{"date": "2099-01-01", "price": "FUTURE-SECRET"}],
               "path": "C:/private/future.json"}, **evidence}
        entry = {"symbol": TICKER, "price": price, "asof": at, "received_at": AT,
                 "source": "fixture", "kind": "fixture", "currency": "CNY", "evidence": raw}
        qid = digest(entry)
        self.db.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?)", (qid, TICKER, price, at, AT,
                        "fixture", "fixture", "CNY", canonical(raw), digest(raw)))
        return qid

    @contextmanager
    def _read(self):
        yield self.db

    def _account(self, db):
        return self.account

    def _now(self, account):
        return account["clock"]

    def _policy(self, db):
        return self.cn


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.trader = MemoryTrader()
        self.addCleanup(self.trader.db.close)

    def doc(self, **changes):
        doc = {"kind": "announcement", "symbol": TICKER, "published_at": AT, "available_at": AT,
               "title": "Synthetic release", "summary": "Known disclosure, untrusted text", "source_hash": digest("raw article")}
        doc.update(changes)
        return doc

    def test_stable_frozen_projection_no_future_raw_or_source_paths(self):
        first = build_observation(self.trader, [TICKER])
        self.assertEqual(first, build_observation(self.trader, [TICKER]))
        serialized = canonical(first)
        for forbidden in ("FUTURE-SECRET", "2099-01-01", "C:/private", "capture", "raw_response"):
            self.assertNotIn(forbidden, serialized.replace("raw captures", "archives"))
        self.assertEqual(len(first["series"][TICKER]), 3)
        self.assertNotIn("cash", first)
        self.assertNotIn("positions", first)

    def test_quote_asof_and_available_cutoff_and_lookback(self):
        self.trader.quote("10.3", "2026-09-24T10:01:00+08:00")
        self.trader.quote("10.4", "2026-09-24T09:59:30+08:00", available_at=timestamp("2026-09-24T10:02:00+08:00"))
        obs = build_observation(self.trader, [TICKER], lookback=2)
        self.assertEqual([r["price"] for r in obs["series"][TICKER]], ["10.1", "10.2"])
        older = build_observation(self.trader, [TICKER], as_of="2026-09-24T09:59:00+08:00")
        self.assertEqual([r["price"] for r in older["series"][TICKER]], ["10", "10.1"])
        with self.assertRaises(ValueError):
            build_observation(self.trader, [TICKER], as_of="2026-09-24T10:01:00+08:00")

    def test_documents_require_published_and_available_before_cutoff(self):
        future = timestamp("2026-09-24T10:01:00+08:00")
        docs = [self.doc(), self.doc(published_at=future, available_at=future, summary="FUTURE-DOC"),
                self.doc(available_at=future, summary="DELAYED-DOC")]
        obs = build_observation(self.trader, [TICKER], documents=docs)
        self.assertEqual(len(obs["documents"]), 1)
        self.assertNotIn("FUTURE-DOC", canonical(obs))
        self.assertNotIn("DELAYED-DOC", canonical(obs))
        for changes in ({"published_at": None}, {"raw": "secret"}, {"summary": "C:/private/file"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_observation(self.trader, [TICKER], documents=[self.doc(**changes)])

    def test_duplicate_capture_does_not_change_signal_rows_and_conflict_refused(self):
        self.trader.quote("10.2", "2026-09-24T10:00:00+08:00", extra_capture="second capture")
        obs = build_observation(self.trader, [TICKER])
        self.assertEqual(len(obs["series"][TICKER]), 3)
        self.trader.quote("10.3", "2026-09-24T10:00:00+08:00", extra_capture="conflict")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            build_observation(self.trader, [TICKER])

    def test_future_metadata_is_absent_not_backfilled(self):
        row = self.trader.db.execute("SELECT payload FROM instruments").fetchone()
        data = json.loads(row[0])
        data["source"]["available_at"] = timestamp("2026-09-24T10:01:00+08:00")
        self.trader.db.execute("UPDATE instruments SET payload=?,hash=?", (canonical(data), digest(data)))
        obs = build_observation(self.trader, [TICKER])
        self.assertEqual(obs["instruments"], {})
        self.assertIn("instrument", [gap["field"] for gap in obs["missing"]])

    def test_public_schema_rejects_extra_raw_and_future_resealed_rows(self):
        obs = build_observation(self.trader, [TICKER])
        for change in ("raw", "future", "cash", "price"):
            bad = copy.deepcopy(obs)
            if change == "raw":
                bad["series"][TICKER][0]["raw"] = "future raw"
            elif change == "future":
                bad["series"][TICKER][-1]["available_at"] = timestamp("2026-09-24T11:00:00+08:00")
            elif change == "price":
                bad["series"][TICKER][0]["price"] = "NaN"
            else:
                bad["cash"] = "10000"
            bad["observation_id"] = digest({k: v for k, v in bad.items() if k != "observation_id"})
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_observation(bad)

    def test_wrong_currency_and_corrupt_metadata_hash_rejected(self):
        self.trader.account["currency"] = "USD"
        with self.assertRaises(ValueError):
            build_observation(self.trader, [TICKER])
        self.trader.account["currency"] = "CNY"
        self.trader.db.execute("UPDATE instruments SET hash=?", ("0" * 64,))
        with self.assertRaisesRegex(ValueError, "hash"):
            build_observation(self.trader, [TICKER])


if __name__ == "__main__":
    unittest.main()
