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


class market_recorder_test_fetcher:
    def __call__(self, url, timeout=12.0):
        if "gamma" in url:
            return [
                {
                    "id": "btc",
                    "question": "Will Bitcoin hit 100k?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.42", "0.58"]',
                    "clobTokenIds": '["tok_yes", "tok_no"]',
                    "volume": "5000",
                }
            ]
        if "clob.polymarket.com/markets" in url:
            return {"markets": []}
        if "data-api.polymarket.com/trades" in url:
            return []
        if "clob.polymarket.com/book" in url:
            return {"bids": [["0.41", "100"]], "asks": [["0.43", "80"]]}
        raise AssertionError(url)


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

    def test_bot_registry_manifest_detects_script_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            self.assertTrue(bot_scripts.verify_bot_registry_manifest(data_dir)["ok"])
            path = bot_scripts.script_path(data_dir, "superwing")
            script = json.loads(path.read_text())
            script["status"] = "tampered_without_manifest_update"
            path.write_text(json.dumps(script), encoding="utf-8")

            verified = bot_scripts.verify_bot_registry_manifest(data_dir)

        self.assertFalse(verified["ok"])
        self.assertIn("bot_script_manifest_hash_mismatch:superwing", verified["errors"])

    def test_bot_loop_missing_manifest_does_not_recreate_and_trade_tampered_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            market_recorder.capture_once(root, fetcher=market_recorder_test_fetcher(), now=lambda: "2026-06-14T03:30:00+00:00", max_books=2)
            data_dir = root / "paper_duel"
            bot_scripts.ensure_default_bot_scripts(data_dir)
            bot_scripts.manifest_path(data_dir).unlink()
            path = bot_scripts.script_path(data_dir, "superwing")
            script = json.loads(path.read_text())
            script["status"] = "valid_but_unmanifested_tamper"
            path.write_text(json.dumps(script), encoding="utf-8")
            args = types.SimpleNamespace(data_dir=str(data_dir), env_file="", mode="paper_apply", limit=1, min_volume=0, max_orders=2, mock_markets="", allow_proxy=False, dashboard_dir="")
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999", "AURUM_DEEPSEEK_ALLOW_PAPER_APPLY": "true", "AURUM_DEEPSEEK_OPERATOR_CONFIRM": "ALLOW_DEEPSEEK_PAPER_APPLY"}, clear=False):
                tick = agent_bot_loop.run_mechanical_tick(args)
                second_tick = agent_bot_loop.run_mechanical_tick(args)
            manifest_exists_after_ticks = bot_scripts.manifest_path(data_dir).exists()

        self.assertFalse(tick["applied"])
        self.assertEqual(tick["effective_mode"], "hold_only")
        self.assertIn("missing_bot_registry_manifest", tick["bot_script_manifest"]["errors"])
        self.assertFalse(second_tick["applied"])
        self.assertEqual(second_tick["effective_mode"], "hold_only")
        self.assertIn("missing_bot_registry_manifest", second_tick["bot_script_manifest"]["errors"])
        self.assertFalse(manifest_exists_after_ticks)

    def test_deepseek_default_bot_script_is_hold_only_until_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            superwing_script = bot_scripts.load_bot_script(data_dir, "superwing")
            deepseek_script = bot_scripts.load_bot_script(data_dir, "deepseek")
            state = agent_duel.init_state(data_dir, reset=True)
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
            superwing_decision = bot_scripts.mechanical_decision_for_agent(state["accounts"]["superwing"], markets, superwing_script)
            deepseek_decision = bot_scripts.mechanical_decision_for_agent(state["accounts"]["deepseek"], markets, deepseek_script)

        self.assertEqual(superwing_decision["orders"][0]["side"], "buy")
        self.assertTrue(deepseek_script["hold_only"])
        self.assertEqual(deepseek_script["status"], "awaiting_validated_strategy")
        self.assertEqual(deepseek_script["allowed_sides"], [])
        self.assertEqual(deepseek_script["max_orders_per_tick"], 0)
        self.assertEqual(deepseek_decision["orders"], [])
        self.assertNotEqual(
            json.dumps(superwing_script, sort_keys=True),
            json.dumps(deepseek_script, sort_keys=True),
        )

    def test_invalid_bot_script_fails_closed_to_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            bot_scripts.script_path(data_dir, "superwing").write_text("{half-json", encoding="utf-8")
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            state = agent_duel.init_state(data_dir, reset=True)
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
                }
            ]
            decision = bot_scripts.mechanical_decision_for_agent(state["accounts"]["superwing"], markets, script)

        self.assertTrue(script["hold_only"])
        self.assertEqual(script["status"], "script_invalid")
        self.assertIn("script_invalid", script["risk_reason"])
        self.assertEqual(script["allowed_sides"], [])
        self.assertEqual(decision["orders"], [])
        self.assertIn("script_invalid", decision["notes"])

    def test_malformed_bot_script_schema_fails_closed_to_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            bot_scripts.script_path(data_dir, "superwing").write_text(
                json.dumps({"allowed_sides": "buy", "buy_when": "not-a-rule-object"}),
                encoding="utf-8",
            )
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            state = agent_duel.init_state(data_dir, reset=True)
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
                }
            ]
            decision = bot_scripts.mechanical_decision_for_agent(state["accounts"]["superwing"], markets, script)

        self.assertTrue(script["hold_only"])
        self.assertEqual(script["status"], "script_invalid")
        self.assertIn("schema", script["risk_reason"])
        self.assertEqual(decision["orders"], [])

    def test_invalid_allowed_sides_list_fails_closed_instead_of_defaulting_to_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            bot_scripts.script_path(data_dir, "superwing").write_text(
                json.dumps({"allowed_sides": ["not-a-side"]}),
                encoding="utf-8",
            )
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            state = agent_duel.init_state(data_dir, reset=True)
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
                }
            ]
            decision = bot_scripts.mechanical_decision_for_agent(state["accounts"]["superwing"], markets, script)

        self.assertTrue(script["hold_only"])
        self.assertEqual(script["status"], "script_invalid")
        self.assertIn("allowed_sides", script["risk_reason"])
        self.assertEqual(decision["orders"], [])

    def test_malformed_nested_buy_rule_fails_closed_instead_of_runtime_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            bot_scripts.script_path(data_dir, "superwing").write_text(
                json.dumps({"buy_when": {"price_min": "low"}}),
                encoding="utf-8",
            )
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            state = agent_duel.init_state(data_dir, reset=True)
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
                }
            ]
            decision = bot_scripts.mechanical_decision_for_agent(state["accounts"]["superwing"], markets, script)

        self.assertTrue(script["hold_only"])
        self.assertEqual(script["status"], "script_invalid")
        self.assertIn("buy_when", script["risk_reason"])
        self.assertEqual(decision["orders"], [])

    def test_non_finite_numeric_rule_fails_closed_to_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            bot_scripts.script_path(data_dir, "superwing").write_text(
                json.dumps({"buy_when": {"max_notional": "NaN"}}),
                encoding="utf-8",
            )
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            state = agent_duel.init_state(data_dir, reset=True)
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
                }
            ]
            decision = bot_scripts.mechanical_decision_for_agent(state["accounts"]["superwing"], markets, script)

        self.assertTrue(script["hold_only"])
        self.assertEqual(script["status"], "script_invalid")
        self.assertIn("max_notional", script["risk_reason"])
        self.assertEqual(decision["orders"], [])

    def test_fractional_integer_script_field_fails_closed_to_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            bot_scripts.script_path(data_dir, "superwing").write_text(
                json.dumps({"max_orders_per_tick": 1.5}),
                encoding="utf-8",
            )
            script = bot_scripts.load_bot_script(data_dir, "superwing")
            state = agent_duel.init_state(data_dir, reset=True)
            markets = [
                {
                    "market_id": "btc-1",
                    "question": "Will Bitcoin close above 100k?",
                    "volume": 10000,
                    "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
                }
            ]
            decision = bot_scripts.mechanical_decision_for_agent(state["accounts"]["superwing"], markets, script)

        self.assertTrue(script["hold_only"])
        self.assertEqual(script["status"], "script_invalid")
        self.assertIn("max_orders_per_tick", script["risk_reason"])
        self.assertEqual(decision["orders"], [])

    def test_tick_exposes_invalid_script_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reports").mkdir(parents=True)
            (root / "normalized" / "polymarket").mkdir(parents=True)
            ts = "2026-06-14T03:30:00+00:00"
            (root / "reports" / "market_recorder_health.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "ts": ts,
                        "sources": {
                            "gamma_markets": {"ok_frames": 1},
                            "clob_markets": {"ok_frames": 1},
                            "data_trades": {"ok_frames": 1},
                            "clob_book": {"ok_frames": 2, "requested_tokens": 2},
                        },
                        "book_coverage": {"requested_tokens": 2, "ok_tokens": 2, "orderable_tokens": 2},
                        "manifest": {"ok": True, "frames": 4},
                    }
                ),
                encoding="utf-8",
            )
            (root / "normalized" / "polymarket" / "latest_markets.json").write_text(
                json.dumps({"ts": ts, "source": "polymarket_market_recorder_v0", "markets": [{"market_id": "btc", "question": "Will BTC?", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}]}),
                encoding="utf-8",
            )
            data_dir = root / "paper_duel"
            bot_scripts.ensure_default_bot_scripts(data_dir)
            bot_scripts.script_path(data_dir, "superwing").write_text("{half-json", encoding="utf-8")
            args = types.SimpleNamespace(
                data_dir=str(data_dir),
                env_file="",
                mode="review_only",
                limit=1,
                min_volume=0,
                max_orders=2,
                mock_markets="",
                allow_proxy=False,
                dashboard_dir="",
            )
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999"}, clear=False):
                tick = agent_bot_loop.run_mechanical_tick(args)

        self.assertEqual(tick["agents"]["superwing"]["script"]["status"], "script_invalid")
        self.assertEqual(tick["agents"]["superwing"]["decision"]["orders"], [])
        self.assertIn("script_invalid", tick["agents"]["superwing"]["decision"]["notes"])

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
                            "clob_book": {"ok_frames": 2, "requested_tokens": 2},
                        },
                        "book_coverage": {"requested_tokens": 2, "ok_tokens": 2, "orderable_tokens": 2},
                        "manifest": {"ok": True, "frames": 4},
                    }
                )
            )
            args = types.SimpleNamespace(limit=1, min_volume=1000, mock_markets="", allow_proxy=False)
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999"}, clear=False):
                loaded, source = agent_bot_loop.load_markets_for_tick(root / "paper_duel", args)

        self.assertEqual(source["source"], "polymarket_market_recorder_v0")
        self.assertEqual([market["market_id"] for market in loaded], ["high"])

    def test_bot_loop_holds_when_recorder_health_is_bad_even_if_latest_markets_exists(self):
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
            args = types.SimpleNamespace(limit=1, min_volume=0, mock_markets="", allow_proxy=False)
            with mock.patch.object(agent_duel, "fetch_markets", side_effect=AssertionError("direct fallback forbidden")) as fetch_markets:
                loaded, source = agent_bot_loop.load_markets_for_tick(root / "paper_duel", args)

        fetch_markets.assert_not_called()
        self.assertEqual(loaded, [])
        self.assertEqual(source["source"], "data_quality_gate")
        self.assertEqual(source["decision"], "HOLD_ONLY")
        self.assertIn("last_capture_not_ok", source["reason_codes"])

    def test_dev_mode_can_direct_fetch_when_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reports").mkdir(parents=True)
            (root / "normalized" / "polymarket").mkdir(parents=True)
            ts = "2026-06-14T03:30:00+00:00"
            (root / "reports" / "market_recorder_health.json").write_text(
                json.dumps({"ok": False, "ts": ts, "sources": {"clob_book": {"ok_frames": 0}}})
            )
            (root / "normalized" / "polymarket" / "latest_markets.json").write_text(
                json.dumps({"ts": ts, "source": "polymarket_market_recorder_v0", "markets": []})
            )
            fallback_markets = [{"market_id": "fallback", "question": "Dev smoke fallback", "volume": 1, "outcomes": [{"name": "Yes", "price": 0.4}, {"name": "No", "price": 0.6}]}]
            args = types.SimpleNamespace(limit=1, min_volume=0, mock_markets="", allow_proxy=False)
            with mock.patch.dict(os.environ, {"AURUM_ALLOW_UNAUDITED_FALLBACK": "true"}, clear=False):
                with mock.patch.object(agent_duel, "fetch_markets", return_value=fallback_markets) as fetch_markets:
                    loaded, source = agent_bot_loop.load_markets_for_tick(root / "paper_duel", args)

        fetch_markets.assert_called_once_with(1, 0, "", False)
        self.assertEqual(loaded, fallback_markets)
        self.assertEqual(source["source"], "unaudited_direct_fetch_fallback")
        self.assertEqual(source["data_quality_gate"]["decision"], "HOLD_ONLY")

    def test_mechanical_tick_records_hold_only_when_data_gate_blocks_paper_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reports").mkdir(parents=True)
            (root / "normalized" / "polymarket").mkdir(parents=True)
            ts = "2026-06-14T03:30:00+00:00"
            (root / "reports" / "market_recorder_health.json").write_text(
                json.dumps(
                    {
                        "ok": False,
                        "ts": ts,
                        "sources": {
                            "gamma_markets": {"ok_frames": 1},
                            "clob_markets": {"ok_frames": 1},
                            "data_trades": {"ok_frames": 1},
                            "clob_book": {"ok_frames": 0},
                        },
                    }
                )
            )
            (root / "normalized" / "polymarket" / "latest_markets.json").write_text(
                json.dumps({"ts": ts, "source": "polymarket_market_recorder_v0", "markets": [{"market_id": "btc", "question": "Will BTC?", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}]})
            )
            args = types.SimpleNamespace(
                data_dir=str(root / "paper_duel"),
                env_file="",
                mode="paper_apply",
                limit=1,
                min_volume=0,
                max_orders=2,
                mock_markets="",
                allow_proxy=False,
                dashboard_dir="",
            )
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999"}, clear=False):
                with mock.patch.object(agent_duel, "fetch_markets", side_effect=AssertionError("direct fallback forbidden")):
                    tick = agent_bot_loop.run_mechanical_tick(args)

        self.assertTrue(tick["ok"])
        self.assertFalse(tick["applied"])
        self.assertEqual(tick["market_count"], 0)
        self.assertEqual(tick["market_source"]["source"], "data_quality_gate")
        self.assertEqual(tick["data_quality_gate"]["decision"], "HOLD_ONLY")
        for agent in agent_duel.AGENTS:
            self.assertEqual(tick["agents"][agent]["decision"]["orders"], [])
            self.assertEqual(tick["agents"][agent]["result"]["fills"], [])

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
