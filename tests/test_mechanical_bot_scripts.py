import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_duel
import bot_scripts
import agent_bot_loop


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
