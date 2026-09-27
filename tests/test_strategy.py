import copy
import unittest

from mercury.common import digest, timestamp
from mercury.observation import build_observation
from mercury.strategy import (compile_intents, validate_intent, validate_research, validate_strategy)
from test_observation import AT, MemoryTrader, TICKER


def seal(value, key):
    result = copy.deepcopy(value)
    result.pop(key, None)
    result[key] = digest(result)
    return result


def research_for(obs):
    return seal({"version": "cn-research-v1", "observation_id": obs["observation_id"], "as_of": obs["as_of"],
        "hypotheses": [{"hypothesis_id": "h1", "statement": "Synthetic rising observations", "invalidation": "Stop if signal reverses",
                        "evidence_ids": [obs["series"][TICKER][-1]["quote_id"]]}]}, "research_id")


def strategy_for(obs):
    research = research_for(obs)
    return seal({"version": "cn-strategy-v1", "observation_id": obs["observation_id"], "research_id": research["research_id"],
        "as_of": obs["as_of"], "universe": [TICKER], "signals": [{"name": "momentum", "window": 2, "weight": "1"}],
        "top_k": 1, "allocation": {"gross_weight": "0.5", "weighting": "equal"},
        "rebalance": {"every_sessions": 1, "anchor_day": "2026-09-24"}, "holding": {"max_sessions": 5},
        "invalidation": {"expires_at": timestamp("2026-09-24T15:00:00+08:00"), "max_quote_age_seconds": 300, "min_signal": "-1"},
        "reason": "Synthetic test rationale", "evidence_ids": research["hypotheses"][0]["evidence_ids"]}, "strategy_id")


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.trader = MemoryTrader()
        self.addCleanup(self.trader.db.close)
        self.obs = build_observation(self.trader, [TICKER])
        self.strategy = strategy_for(self.obs)
        self.portfolio = {"currency": "CNY", "time": AT, "equity": "100000", "positions": [], "missing_marks": []}

    def test_research_and_strategy_strict_evidence_link(self):
        research = research_for(self.obs)
        self.assertEqual(validate_research(research, self.obs), research)
        self.assertEqual(validate_strategy(self.strategy, self.obs, research), self.strategy)
        bad = copy.deepcopy(research)
        bad["hypotheses"][0]["evidence_ids"] = ["0" * 64]
        with self.assertRaises(ValueError):
            validate_research(seal(bad, "research_id"), self.obs)
        bad = copy.deepcopy(research)
        bad["hypotheses"][0]["statement"] = ""
        with self.assertRaises(ValueError):
            validate_research(seal(bad, "research_id"), self.obs)

    def test_deterministic_lot_allocation_only_intent_not_fill(self):
        intent = compile_intents(self.strategy, self.obs, self.portfolio)
        self.assertEqual(intent, compile_intents(self.strategy, self.obs, self.portfolio))
        self.assertEqual(intent["orders"], [{"symbol": TICKER, "side": "buy", "qty": 4900, "limit_price": "10.2", "target_weight": "0.5"}])
        self.assertEqual(validate_intent(intent, self.obs, self.strategy), intent)
        self.assertEqual(self.portfolio["positions"], [])

    def test_unknown_fields_arbitrary_code_and_nonfinite_rejected(self):
        for mutation in ("extra", "eval", "nan", "future", "wrong_obs", "empty_reason", "weights"):
            bad = copy.deepcopy(self.strategy)
            if mutation == "extra":
                bad["command"] = "python -c attack"
            elif mutation == "eval":
                bad["signals"][0]["name"] = "__import__('os').system"
            elif mutation == "nan":
                bad["allocation"]["gross_weight"] = "NaN"
            elif mutation == "future":
                bad["as_of"] = timestamp("2026-09-24T10:01:00+08:00")
            elif mutation == "wrong_obs":
                bad["observation_id"] = "0" * 64
            elif mutation == "empty_reason":
                bad["reason"] = ""
            else:
                bad["signals"][0]["weight"] = "0.9"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_strategy(seal(bad, "strategy_id"), self.obs)

    def test_sma_and_volatility_allowed_but_mixed_frequencies_refused(self):
        for name in ("sma", "volatility"):
            strategy = copy.deepcopy(self.strategy)
            strategy["signals"][0]["name"] = name
            self.assertIn("orders", compile_intents(seal(strategy, "strategy_id"), self.obs, self.portfolio))
        bad = copy.deepcopy(self.obs)
        bad["series"][TICKER][0]["frequency"] = "day"
        bad = seal(bad, "observation_id")
        with self.assertRaisesRegex(ValueError, "frequency"):
            compile_intents(strategy_for(bad), bad, self.portfolio)

    def test_stale_valuation_wrong_currency_and_time_fail(self):
        for changes in ({"currency": "USD"}, {"time": timestamp("2026-09-24T10:01:00+08:00")},
                        {"missing_marks": [TICKER]}, {"equity": None}):
            portfolio = dict(self.portfolio, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                compile_intents(self.strategy, self.obs, portfolio)
        strategy = copy.deepcopy(self.strategy)
        strategy["signals"][0]["window"] = 3
        with self.assertRaisesRegex(ValueError, "insufficient"):
            compile_intents(seal(strategy, "strategy_id"), self.obs, self.portfolio)

    def test_t1_sellability_and_holding_session_evidence_required(self):
        strategy = copy.deepcopy(self.strategy)
        strategy["allocation"]["gross_weight"] = "0"
        strategy = seal(strategy, "strategy_id")
        position = {"symbol": TICKER, "qty": 100, "holding_sessions": 5, "available_to_sell": 0}
        portfolio = dict(self.portfolio, positions=[position])
        with self.assertRaisesRegex(ValueError, r"T\+1"):
            compile_intents(strategy, self.obs, portfolio)
        position["available_to_sell"] = 100
        intent = compile_intents(strategy, self.obs, portfolio)
        self.assertEqual(intent["orders"][0]["side"], "sell")
        position.pop("holding_sessions")
        with self.assertRaisesRegex(ValueError, "holding-session"):
            compile_intents(strategy, self.obs, portfolio)

    def test_below_lot_allocation_is_explicit_hold(self):
        portfolio = dict(self.portfolio, equity="100")
        intent = compile_intents(self.strategy, self.obs, portfolio)
        self.assertEqual(intent["orders"], [])
        self.assertIn("minimum lot", intent["reason"])

    def test_real_live_portfolio_receipt_is_bounded_and_not_rewritten(self):
        account_time = timestamp("2026-09-24T10:00:02+08:00")
        portfolio = dict(self.portfolio, mode="live", time=account_time)
        intent = compile_intents(self.strategy, self.obs, portfolio)
        self.assertEqual(intent["as_of"], AT)
        self.assertEqual(portfolio["time"], account_time)
        for at in ("2026-09-24T09:59:59+08:00", "2026-09-24T10:00:06+08:00"):
            with self.subTest(at=at), self.assertRaises(ValueError):
                compile_intents(self.strategy, self.obs, dict(portfolio, time=timestamp(at)))

    def test_hold_expiry_forces_exit_and_prevents_rebuy(self):
        portfolio = dict(self.portfolio, positions=[{"symbol": TICKER, "qty": 100,
            "holding_sessions": 5, "available_to_sell": 100}])
        intent = compile_intents(self.strategy, self.obs, portfolio)
        self.assertEqual(intent["orders"], [{"symbol": TICKER, "side": "sell", "qty": 100,
                                           "limit_price": "10.2", "target_weight": "0"}])

    def test_rebalance_uses_verified_sessions_not_natural_day_difference(self):
        obs = copy.deepcopy(self.obs)
        obs["calendar"]["trading_days"] = ["2026-09-22", "2026-09-23", "2026-09-24"]
        obs = seal(obs, "observation_id")
        strategy = strategy_for(obs)
        strategy["rebalance"] = {"every_sessions": 3, "anchor_day": "2026-09-22"}
        strategy = seal(strategy, "strategy_id")
        intent = compile_intents(strategy, obs, self.portfolio)
        self.assertEqual(intent["orders"], [])
        self.assertIn("not due", intent["reason"])

    def test_future_expiry_not_future_decision_and_excessive_scale_rejected(self):
        strategy = copy.deepcopy(self.strategy)
        strategy["invalidation"]["expires_at"] = AT
        with self.assertRaises(ValueError):
            validate_strategy(seal(strategy, "strategy_id"), self.obs)
        strategy = copy.deepcopy(self.strategy)
        strategy["allocation"]["gross_weight"] = "1e-10000000"
        with self.assertRaises(ValueError):
            validate_strategy(seal(strategy, "strategy_id"), self.obs)

    def test_intent_has_no_execution_price_and_rejects_tampering(self):
        intent = compile_intents(self.strategy, self.obs, self.portfolio)
        for mutation in ("fill_price", "qty", "tick", "future", "reference"):
            bad = copy.deepcopy(intent)
            if mutation == "fill_price":
                bad["orders"][0]["fill_price"] = "1"
            elif mutation == "qty":
                bad["orders"][0]["qty"] = True
            elif mutation == "tick":
                bad["orders"][0]["limit_price"] = "10.201"
            elif mutation == "future":
                bad["as_of"] = timestamp("2026-09-24T10:01:00+08:00")
            else:
                bad["evidence_ids"] = ["0" * 64]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_intent(seal(bad, "intent_id"), self.obs, self.strategy)


if __name__ == "__main__":
    unittest.main()
