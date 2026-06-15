import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cex_arena


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


if __name__ == "__main__":
    unittest.main()
