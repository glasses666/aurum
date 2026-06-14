import argparse
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_duel
import bot_scripts
import generate_dashboard


class DashboardDataQualityTests(unittest.TestCase):
    def test_dashboard_shows_deepseek_awaiting_validated_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        self.assertIn("awaiting_validated_strategy", html)
        self.assertIn("DeepSeek", html)

    def test_dashboard_warns_when_latest_tick_used_unaudited_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            (data_dir / "ticks.jsonl").write_text(
                '{"tick_id":"t-fallback","ts":"2026-06-14T03:30:00+00:00","mode":"review_only","loop_interval_sec":15,"market_source":{"source":"unaudited_direct_fetch_fallback","data_quality_gate":{"decision":"HOLD_ONLY","reason_codes":["last_capture_not_ok"],"recorder_age_seconds":240}},"scores":[]}\n',
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        self.assertIn("Data quality", html)
        self.assertIn("unaudited_direct_fetch_fallback", html)
        self.assertIn("last_capture_not_ok", html)
        self.assertIn("fallback ticks last 1h", html)

    def test_dashboard_shows_actual_vs_configured_interval_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            env_file = root / ".env"
            env_file.write_text("AURUM_BOT_DEFAULT_INTERVAL_SEC=3600\n", encoding="utf-8")
            agent_duel.init_state(data_dir, reset=True)
            (data_dir / "ticks.jsonl").write_text(
                '{"tick_id":"t-fast","ts":"2026-06-14T03:30:00+00:00","mode":"review_only","loop_interval_sec":15,"market_source":{"source":"polymarket_market_recorder_v0"},"scores":[]}\n',
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file=str(env_file), output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        self.assertIn("actual 15s", html)
        self.assertIn("configured 3600s", html)
        self.assertIn("interval mismatch", html)

    def test_dashboard_does_not_infer_trade_allowed_without_gate_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            (data_dir / "ticks.jsonl").write_text(
                '{"tick_id":"legacy","ts":"2026-06-14T03:30:00+00:00","mode":"review_only","loop_interval_sec":15,"market_source":{"source":"polymarket_market_recorder_v0"},"scores":[]}\n',
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        self.assertIn("UNKNOWN · no gate", html)
        self.assertNotIn("TRADE_ALLOWED", html)

    def test_dashboard_shows_recorder_manifest_book_and_universe_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            (data_dir / "ticks.jsonl").write_text(
                (
                    '{"tick_id":"fresh","ts":"2026-06-14T03:30:00+00:00","mode":"paper_apply",'
                    '"loop_interval_sec":15,'
                    '"market_source":{"source":"polymarket_market_recorder_v0"},'
                    '"data_quality_gate":{"decision":"TRADE_ALLOWED","reason_codes":[],"recorder_age_seconds":12,'
                    '"manifest_verification_scope":"tail","manifest_verification_max_rows":500,'
                    '"manifest_verification_verified_rows":4,"book_coverage":{"requested_tokens":2,"ok_tokens":2},'
                    '"orderable_market_count":1,"universe":"bitcoin"},'
                    '"scores":[]}\n'
                ),
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        self.assertIn("TRADE_ALLOWED · clear", html)
        self.assertIn("tail max 500 verified 4", html)
        self.assertIn("2/2 · orderable markets 1", html)
        self.assertIn("yes · universe bitcoin", html)

    def test_dashboard_treats_btc_alias_as_btc_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            (data_dir / "ticks.jsonl").write_text(
                (
                    '{"tick_id":"fresh","ts":"2026-06-14T03:30:00+00:00","mode":"paper_apply",'
                    '"loop_interval_sec":15,'
                    '"market_source":{"source":"polymarket_market_recorder_v0"},'
                    '"data_quality_gate":{"decision":"TRADE_ALLOWED","reason_codes":[],"universe":"btc"},'
                    '"scores":[]}\n'
                ),
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        self.assertIn("yes · universe btc", html)


if __name__ == "__main__":
    unittest.main()
