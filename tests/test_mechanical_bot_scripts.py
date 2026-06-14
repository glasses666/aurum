import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_duel
import bot_scripts
import agent_bot_loop
import market_recorder
import strategy_rules


class MechanicalBotScriptTests(unittest.TestCase):
    def test_default_bot_script_is_mechanical_and_has_buy_sell_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            script = bot_scripts.load_bot_script(data_dir, "superwing")

        self.assertEqual(script["agent_id"], "superwing")
        self.assertEqual(script["execution_mode"], "mechanical_script")
        self.assertEqual(script["interval_sec"], 15)
        self.assertEqual(script["min_interval_sec"], 5)
        self.assertEqual(script["allowed_sides"], ["buy", "sell"])
        self.assertIn("buy_when", script)
        self.assertIn("sell_when", script)
        self.assertIn("take_profit_pct", script["sell_when"])
        self.assertIn("stop_loss_pct", script["sell_when"])

    def test_agent_layer_can_write_mechanical_script_for_bot_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            path = bot_scripts.write_bot_script(
                data_dir,
                "deepseek",
                {
                    "interval_sec": 1,
                    "buy_when": {"price_min": 0.2, "price_max": 0.4, "max_notional": 7},
                    "sell_when": {"take_profit_pct": 0.05, "stop_loss_pct": 0.03},
                },
                source="review:test",
            )
            script = bot_scripts.load_bot_script(data_dir, "deepseek")
            self.assertTrue(path.exists())

        self.assertEqual(script["generated_by"], "review:test")
        self.assertEqual(script["execution_mode"], "mechanical_script")
        self.assertEqual(script["interval_sec"], 5)
        self.assertEqual(script["buy_when"]["max_notional"], 7)
        self.assertEqual(script["sell_when"]["take_profit_pct"], 0.05)

    def test_resident_loop_interval_is_clamped_to_hard_minimum(self):
        self.assertEqual(agent_bot_loop.effective_interval({"interval_sec": 1, "min_interval_sec": 5}), 5)
        self.assertEqual(agent_bot_loop.effective_interval({"interval_sec": 15, "min_interval_sec": 5}), 15)

    def test_mechanical_decision_buys_from_script_without_calling_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            state = agent_duel.init_state(data_dir, reset=True)
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "liquidity": 1000,
                    "outcomes": [
                        {"name": "Yes", "price": 0.42},
                        {"name": "No", "price": 0.58},
                    ],
                }
            ]

            decision = bot_scripts.mechanical_decision_for_agent(
                state["accounts"]["superwing"], markets, script
            )

        self.assertEqual(decision["agent_id"], "superwing")
        self.assertEqual(decision["source"], "mechanical_script")
        self.assertEqual(decision["orders"][0]["side"], "buy")
        self.assertEqual(decision["orders"][0]["market_id"], "btc-1")

    def test_mechanical_decision_sells_on_take_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            state = agent_duel.init_state(data_dir, reset=True)
            account = state["accounts"]["superwing"]
            account["positions"]["btc-1::Yes"] = {
                "market_id": "btc-1",
                "question": "Will Bitcoin close above 100k?",
                "outcome": "Yes",
                "shares": 10.0,
                "cost_basis": 4.0,
                "avg_price": 0.4,
                "last_price": 0.4,
                "fees_paid": 0.0,
                "created_at": agent_duel.utc_now(),
                "updated_at": agent_duel.utc_now(),
            }
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "liquidity": 1000,
                    "outcomes": [
                        {"name": "Yes", "price": 0.5},
                        {"name": "No", "price": 0.5},
                    ],
                }
            ]

            decision = bot_scripts.mechanical_decision_for_agent(account, markets, script)

        self.assertEqual(decision["orders"][0]["side"], "sell")
        self.assertEqual(decision["orders"][0]["reason"], "take_profit")

    def test_mechanical_decision_does_not_buy_again_in_same_tick_after_sell(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            state = agent_duel.init_state(data_dir, reset=True)
            account = state["accounts"]["deepseek"]
            account["positions"]["btc-1::Yes"] = {
                "market_id": "btc-1",
                "question": "Will Bitcoin close above 100k?",
                "outcome": "Yes",
                "shares": 10.0,
                "cost_basis": 4.0,
                "avg_price": 0.4,
                "last_price": 0.4,
                "fees_paid": 0.0,
                "created_at": agent_duel.utc_now(),
                "updated_at": agent_duel.utc_now(),
            }
            script = bot_scripts.load_bot_script(data_dir, "deepseek")
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "liquidity": 1000,
                    "outcomes": [
                        {"name": "Yes", "price": 0.5},
                        {"name": "No", "price": 0.5},
                    ],
                },
                {
                    "market_id": "btc-2",
                    "question": "Will Bitcoin close above 110k?",
                    "volume": 10000,
                    "liquidity": 1000,
                    "outcomes": [
                        {"name": "Yes", "price": 0.42},
                        {"name": "No", "price": 0.58},
                    ],
                },
            ]

            decision = bot_scripts.mechanical_decision_for_agent(account, markets, script)

        self.assertEqual([order["side"] for order in decision["orders"]], ["sell"])

    def test_mechanical_buy_skips_when_new_order_would_breach_market_cap_with_fee(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            state = agent_duel.init_state(data_dir, reset=True)
            account = state["accounts"]["superwing"]
            account["positions"]["btc-1::Yes"] = {
                "market_id": "btc-1",
                "question": "Will Bitcoin close above 100k?",
                "outcome": "Yes",
                "shares": 400.0,
                "cost_basis": 115.0,
                "avg_price": 0.2875,
                "last_price": 0.28,
                "fees_paid": 0.0,
                "created_at": agent_duel.utc_now(),
                "updated_at": agent_duel.utc_now(),
            }
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "category": "Crypto",
                    "volume": 10000,
                    "liquidity": 1000,
                    "outcomes": [
                        {"name": "Yes", "price": 0.28},
                        {"name": "No", "price": 0.72},
                    ],
                }
            ]

            decision = bot_scripts.mechanical_decision_for_agent(account, markets, script)

        self.assertEqual(decision["orders"], [])

    def test_mechanical_buy_skips_recently_traded_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            state = agent_duel.init_state(data_dir, reset=True)
            account = state["accounts"]["deepseek"]
            account["trades"].append(
                {
                    "ts": agent_duel.utc_now(),
                    "market_id": "btc-1",
                    "outcome": "Yes",
                    "side": "buy",
                    "notional": 10.0,
                }
            )
            script = bot_scripts.load_bot_script(data_dir, "deepseek")
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "category": "Crypto",
                    "volume": 10000,
                    "liquidity": 1000,
                    "outcomes": [
                        {"name": "Yes", "price": 0.28},
                        {"name": "No", "price": 0.72},
                    ],
                }
            ]

            decision = bot_scripts.mechanical_decision_for_agent(account, markets, script)

        self.assertEqual(decision["orders"], [])

    def test_bot_loop_consumes_only_healthy_recorder_markets_with_tick_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "normalized" / "polymarket").mkdir(parents=True)
            (root / "reports").mkdir(parents=True)
            ts = "2026-06-14T03:30:00+00:00"
            markets = [
                {"market_id": "low", "question": "Will Bitcoin stay quiet?", "volume": 10, "outcomes": [{"name": "Yes", "price": 0.4}, {"name": "No", "price": 0.6}]},
                {"market_id": "high", "question": "Will Bitcoin hit 100k?", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]},
                {"market_id": "second", "question": "Will Bitcoin hit 120k?", "volume": 7000, "outcomes": [{"name": "Yes", "price": 0.35}, {"name": "No", "price": 0.65}]},
            ]
            (root / "normalized" / "polymarket" / "latest_markets.json").write_text(
                json.dumps({"ts": ts, "source": "polymarket_market_recorder_v0", "markets": markets})
            )
            (root / "reports" / "market_recorder_health.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "ts": ts,
                        "sources": {
                            "gamma_markets": {"ok_frames": 1},
                            "clob_markets": {"ok_frames": 1},
                            "data_trades": {"ok_frames": 1},
                            "clob_book": {"ok_frames": 1},
                        },
                    }
                )
            )
            args = types.SimpleNamespace(limit=1, min_volume=1000, mock_markets="", allow_proxy=False)
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999"}, clear=False):
                loaded, source = agent_bot_loop.load_markets_for_tick(root / "paper_duel", args)

        self.assertEqual(source["source"], "polymarket_market_recorder_v0")
        self.assertEqual([market["market_id"] for market in loaded], ["high"])

    def test_bot_loop_falls_back_when_recorder_health_is_bad_even_if_latest_markets_exists(self):
        class BrokenBookFetcher:
            def __call__(self, url, timeout=12.0):
                if "gamma" in url:
                    return [
                        {
                            "id": "btc",
                            "question": "Will Bitcoin hit 100k?",
                            "outcomes": '["Yes", "No"]',
                            "outcomePrices": '["0.42", "0.58"]',
                            "clobTokenIds": '["tok_yes", "tok_no"]',
                        }
                    ]
                if "clob.polymarket.com/markets" in url:
                    return {"markets": []}
                if "data-api.polymarket.com/trades" in url:
                    return []
                if "clob.polymarket.com/book" in url:
                    raise RuntimeError("book unavailable")
                raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            market_recorder.capture_once(root, fetcher=BrokenBookFetcher(), now=lambda: "2026-06-14T03:30:00+00:00", max_books=1)
            self.assertFalse(json.loads((root / "reports" / "market_recorder_health.json").read_text())["ok"])
            fallback_markets = [{"market_id": "fallback", "question": "Will Bitcoin recover?", "volume": 1, "outcomes": [{"name": "Yes", "price": 0.4}, {"name": "No", "price": 0.6}]}]
            args = types.SimpleNamespace(limit=1, min_volume=0, mock_markets="", allow_proxy=False)
            with mock.patch.object(agent_duel, "fetch_markets", return_value=fallback_markets) as fetch_markets:
                loaded, source = agent_bot_loop.load_markets_for_tick(root / "paper_duel", args)

        fetch_markets.assert_called_once_with(1, 0, "", False)
        self.assertEqual(source["source"], "direct_fetch_fallback")
        self.assertEqual(loaded, fallback_markets)

    def test_superwing_rules_do_not_accept_schema_echo_placeholders(self):
        rules = strategy_rules.normalize_superwing_rules(
            {
                "name": "name: string",
                "selection": "selection: string",
                "notes": "notes: text",
                "review_rationale": "why this rule change helps",
                "price_min": 0.31,
                "price_max": 0.49,
            }
        )

        self.assertEqual(rules["name"], strategy_rules.DEFAULT_SUPERWING_RULES["name"])
        self.assertEqual(rules["selection"], strategy_rules.DEFAULT_SUPERWING_RULES["selection"])
        self.assertEqual(rules["notes"], strategy_rules.DEFAULT_SUPERWING_RULES["notes"])
        self.assertNotIn("review_rationale", rules)
        self.assertEqual(rules["price_min"], 0.31)

    def test_deepseek_rules_reject_schema_echo_placeholder(self):
        with self.assertRaises(ValueError):
            strategy_rules.validate_deepseek_rules(
                "markdown rules for DeepSeek, paper-only/buy-only/hold-if-no-edge"
            )
        with self.assertRaises(ValueError):
            strategy_rules.validate_deepseek_rules(
                "deepseek_rules_md: markdown rules for DeepSeek, paper-only/buy-only/hold-if-no-edge"
            )

    def test_validate_and_apply_sell_reduces_position_and_increases_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            state = agent_duel.init_state(data_dir, reset=True)
            account = state["accounts"]["superwing"]
            starting_cash = account["cash"]
            account["positions"]["btc-1::Yes"] = {
                "market_id": "btc-1",
                "question": "Will Bitcoin close above 100k?",
                "outcome": "Yes",
                "shares": 10.0,
                "cost_basis": 4.0,
                "avg_price": 0.4,
                "last_price": 0.4,
                "fees_paid": 0.0,
                "created_at": agent_duel.utc_now(),
                "updated_at": agent_duel.utc_now(),
            }
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "category": "Crypto",
                    "volume": 10000,
                    "liquidity": 1000,
                    "outcomes": [
                        {"name": "Yes", "price": 0.5},
                        {"name": "No", "price": 0.5},
                    ],
                }
            ]
            decision = {
                "agent_id": "superwing",
                "orders": [
                    {
                        "market_id": "btc-1",
                        "outcome": "Yes",
                        "side": "sell",
                        "shares": 5.0,
                        "limit_price": 0.45,
                        "rationale": "take profit",
                    }
                ],
            }

            result = agent_duel.validate_and_apply(
                data_dir,
                state,
                "superwing",
                decision,
                markets,
                apply=True,
                max_orders=2,
                max_notional_per_order=45.0,
            )
            updated = agent_duel.load_state(data_dir)["accounts"]["superwing"]

        self.assertEqual(result["rejections"], [])
        self.assertEqual(result["fills"][0]["side"], "sell")
        self.assertLess(updated["positions"]["btc-1::Yes"]["shares"], 10.0)
        self.assertGreater(updated["cash"], starting_cash)


if __name__ == "__main__":
    unittest.main()
