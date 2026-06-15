import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cex_arena
import generate_dashboard


class CexAdapterArenaTests(unittest.TestCase):
    def test_okx_snapshot_normalizes_ticker_book_candles_and_funding(self):
        calls = []

        def fake_fetch(url, timeout=12.0, method="GET", payload=None):
            calls.append(url)
            if "/market/ticker" in url:
                return {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "last": "65000.1", "bidPx": "64999.9", "askPx": "65000.3", "vol24h": "1234"}]}
            if "/market/books" in url:
                return {"code": "0", "data": [{"bids": [["64999.8", "10"]], "asks": [["65000.4", "8"]]}]}
            if "/market/candles" in url:
                return {"code": "0", "data": [["1781524800000", "64900", "65100", "64800", "65000", "100", "6500000", "6500000", "1"]]}
            if "/public/funding-rate" in url:
                return {"code": "0", "data": [{"fundingRate": "0.0001", "nextFundingTime": "1781553600000"}]}
            raise AssertionError(url)

        frame = cex_arena.fetch_exchange_snapshot("okx", "BTC-USDT-SWAP", fetcher=fake_fetch, now=lambda: "2026-06-15T12:00:00+00:00")

        self.assertEqual(frame["source"], "cex_public_api_v0")
        self.assertEqual(frame["exchange"], "okx")
        self.assertEqual(frame["symbol"], "BTC-USDT-SWAP")
        self.assertEqual(frame["market_type"], "cex_perp")
        self.assertEqual(frame["last"], 65000.1)
        self.assertEqual(frame["best_bid"], 64999.8)
        self.assertEqual(frame["best_ask"], 65000.4)
        self.assertAlmostEqual(frame["mid"], 65000.1)
        self.assertEqual(frame["funding_rate"], 0.0001)
        self.assertEqual(frame["candles"][0]["close"], 65000.0)
        self.assertTrue(all("api_key" not in call.lower() for call in calls))

    def test_record_snapshot_writes_normalized_cex_latest_markets(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            frame = {
                "source": "cex_public_api_v0",
                "ts": "2026-06-15T12:00:00+00:00",
                "exchange": "okx",
                "symbol": "BTC-USDT-SWAP",
                "market_id": "okx:BTC-USDT-SWAP",
                "market_type": "cex_perp",
                "last": 65000.0,
                "best_bid": 64999.0,
                "best_ask": 65001.0,
                "mid": 65000.0,
                "funding_rate": 0.0001,
                "fee_bps": 5.0,
            }

            result = cex_arena.record_snapshot(data_dir, frame)
            latest = json.loads((data_dir / "normalized" / "cex" / "latest_markets.json").read_text())
            health = json.loads((data_dir / "reports" / "cex_recorder_health.json").read_text())

        self.assertEqual(result["source"], "cex_public_api_v0")
        self.assertEqual(latest["markets"][0]["market_id"], "okx:BTC-USDT-SWAP")
        self.assertEqual(latest["markets"][0]["market_type"], "cex_perp")
        self.assertTrue(health["ok"])

    def test_paper_order_buy_and_reduce_sell_updates_exchange_account(self):
        account = cex_arena.new_exchange_account("superwing", starting_cash=1000.0)
        frame = {
            "source": "cex_public_api_v0",
            "exchange": "okx",
            "symbol": "BTC-USDT-SWAP",
            "market_type": "cex_perp",
            "best_bid": 99.0,
            "best_ask": 101.0,
            "last": 100.0,
            "mid": 100.0,
            "fee_bps": 5.0,
        }

        buy = cex_arena.apply_paper_order(
            account,
            frame,
            {"side": "buy", "notional": 100.0, "rationale": "test long"},
            now=lambda: "2026-06-15T12:00:01+00:00",
        )
        self.assertTrue(buy["ok"])
        self.assertEqual(len(account["trades"]), 1)
        self.assertGreater(account["positions"]["okx:BTC-USDT-SWAP"]["qty"], 0)
        self.assertLess(account["cash"], 900.0)

        sell = cex_arena.apply_paper_order(
            account,
            frame,
            {"side": "sell", "reduce_only": True, "notional": 50.0},
            now=lambda: "2026-06-15T12:01:01+00:00",
        )
        self.assertTrue(sell["ok"])
        self.assertEqual(len(account["trades"]), 2)
        self.assertGreater(account["cash"], 900.0)
        self.assertGreaterEqual(account["realized_pnl"], -5.0)

    def test_live_trade_guard_blocks_private_exchange_credentials(self):
        env = {"OKX_API_KEY": "x", "AURUM_CEX_ENABLE_LIVE_TRADING": "false"}
        with self.assertRaises(cex_arena.CexSafetyError):
            cex_arena.assert_public_data_only(env)

    def test_paper_order_rejects_short_when_shorting_disabled(self):
        account = cex_arena.new_exchange_account("deepseek", starting_cash=1000.0)
        frame = {"exchange": "okx", "symbol": "ETH-USDT-SWAP", "best_bid": 1999.0, "best_ask": 2001.0, "fee_bps": 5.0}

        result = cex_arena.apply_paper_order(
            account,
            frame,
            {"side": "sell", "notional": 100.0},
            allow_short=False,
            now=lambda: "2026-06-15T12:02:01+00:00",
        )

        self.assertFalse(result["ok"])
        self.assertIn("shorting disabled", result["reason"])
        self.assertEqual(account["trades"], [])

    def test_record_frames_writes_multi_symbol_cex_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            frames = [
                cex_frame("okx", "BTC-USDT-SWAP", 65000.0, close_values=[64900, 65000, 65100]),
                cex_frame("okx", "ETH-USDT-SWAP", 3500.0, close_values=[3490, 3500, 3510]),
            ]

            health = cex_arena.record_frames(data_dir, frames)
            latest = json.loads((data_dir / "normalized" / "cex" / "latest_markets.json").read_text())

        self.assertTrue(health["ok"])
        self.assertEqual(health["market_count"], 2)
        self.assertEqual(latest["market_count"], 2)
        self.assertEqual({m["symbol"] for m in latest["markets"]}, {"BTC-USDT-SWAP", "ETH-USDT-SWAP"})
        self.assertEqual(latest["book_coverage"], {"requested_tokens": 2, "ok_tokens": 2})

    def test_cex_baseline_strategies_emit_gated_decisions(self):
        account = cex_arena.new_exchange_account("momentum", starting_cash=1000.0)
        frame = cex_frame("okx", "BTC-USDT-SWAP", 65000.0, close_values=[64000, 64200, 64500, 65000])

        decisions = cex_arena.baseline_strategy_decisions(frame, account, notional=50.0)

        names = {d["strategy"] for d in decisions}
        self.assertEqual(
            names,
            {"momentum", "mean_reversion", "volatility_breakout", "funding_filter", "grid_shadow"},
        )
        momentum = next(d for d in decisions if d["strategy"] == "momentum")
        self.assertEqual(momentum["decision"], "buy")
        self.assertTrue(momentum["orders"])
        for decision in decisions:
            self.assertTrue(decision["paper_only"])
            self.assertIn(decision["decision"], {"buy", "sell", "hold"})
            self.assertLessEqual(len(decision["orders"]), 1)

    def test_run_cex_tick_updates_strategy_accounts_and_runtime_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            frames = [cex_frame("okx", "BTC-USDT-SWAP", 65000.0, close_values=[64000, 64200, 64500, 65000])]
            cex_arena.record_frames(data_dir, frames)

            tick = cex_arena.run_cex_tick(
                data_dir,
                strategies=["momentum", "mean_reversion"],
                now=lambda: "2026-06-15T12:03:00+00:00",
            )
            state = json.loads((data_dir / "cex_paper" / "state.json").read_text())
            status = json.loads((data_dir / "reports" / "cex_runtime_status.json").read_text())
            ticks = (data_dir / "cex_paper" / "ticks.jsonl").read_text().splitlines()
            decisions = (data_dir / "cex_paper" / "decisions.jsonl").read_text().splitlines()
            risk_rows = (data_dir / "cex_paper" / "risk_ledger.jsonl").read_text().splitlines()

        self.assertTrue(tick["ok"])
        self.assertEqual(tick["runner"], "cex_resident_mechanical_bot_loop")
        self.assertEqual(status["completion_state"], "runtime-complete")
        self.assertEqual(status["data_quality_gate"]["decision"], "TRADE_ALLOWED")
        self.assertIn("momentum", state["accounts"])
        self.assertEqual(len(ticks), 1)
        self.assertEqual(len(decisions), 2)
        self.assertGreaterEqual(len(risk_rows), 1)

    def test_dashboard_manifest_includes_public_cex_terminal_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            paper_dir = root / "paper_duel"
            cex_dir = root / "cex_arena"
            out_dir = root / "dashboard"
            frames = [cex_frame("okx", "BTC-USDT-SWAP", 65000.0, close_values=[64000, 64200, 64500, 65000])]
            cex_arena.record_frames(cex_dir, frames)
            cex_arena.run_cex_tick(cex_dir, strategies=["momentum"], now=lambda: "2026-06-15T12:04:00+00:00")

            args = type("Args", (), {"data_dir": str(paper_dir), "env_file": "", "output_dir": str(out_dir), "operator_output_dir": ""})()
            generate_dashboard.render(args)
            html = (out_dir / "index.html").read_text()
            manifest = json.loads((out_dir / "manifest.json").read_text())

        self.assertIn("Aurum CEX Paper Arena", html)
        self.assertIn("CEX 传统量化 paper canary", html)
        self.assertIn("CEX Traditional Quant", html)
        self.assertEqual(manifest["cex"]["completion_state"], "runtime-complete")
        self.assertEqual(manifest["cex"]["latest_market"]["exchange"], "okx")
        self.assertNotIn("proposal_controls", html)
        self.assertNotIn("/opt/aurum", html)


def cex_frame(exchange, symbol, last, close_values):
    candles = []
    for idx, close in enumerate(close_values):
        candles.append(
            {
                "ts_ms": 1781524800000 + idx * 60000,
                "open": close - 10,
                "high": close + 20,
                "low": close - 20,
                "close": close,
                "volume": 100 + idx,
            }
        )
    return {
        "source": "cex_public_api_v0",
        "ts": "2026-06-15T12:00:00+00:00",
        "exchange": exchange,
        "symbol": symbol,
        "market_id": f"{exchange}:{symbol}",
        "question": f"{symbol} perpetual price on {exchange.upper()}",
        "market_type": "cex_perp",
        "last": last,
        "best_bid": last - 1,
        "best_ask": last + 1,
        "mid": last,
        "spread_bps": 0.3,
        "funding_rate": 0.0001,
        "volume_24h": 1234,
        "candles": candles,
        "fee_bps": 5.0,
    }


if __name__ == "__main__":
    unittest.main()
