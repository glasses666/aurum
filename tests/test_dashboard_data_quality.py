import argparse
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_duel
import aurum_status_report
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

    def test_public_dashboard_uses_coarse_aggregate_output_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            tick = {
                "tick_id": "public-safe",
                "ts": "2026-06-14T03:30:00+00:00",
                "mode": "paper_apply",
                "effective_mode": "paper_apply",
                "applied": True,
                "loop_interval_sec": 15,
                "snapshot_file": "/Users/example/raw-snapshot-secret.json",
                "market_count": 1,
                "market_source": {"source": "polymarket_market_recorder_v0"},
                "data_quality_gate": {"decision": "TRADE_ALLOWED", "reason_codes": []},
                "scores": [
                    {
                        "agent_id": "superwing",
                        "score": 1510.0,
                        "portfolio_value": 1510.0,
                        "cash": 1490.0,
                        "roi": 0.0066,
                        "details": [{"key": "private-position-key", "shares": 12.3456, "mark": 0.42, "value": 5.18}],
                    }
                ],
                "agents": {
                    "superwing": {
                        "decision": {
                            "orders": [
                                {
                                    "market_id": "private-market-key",
                                    "question": "private-question",
                                    "rationale": "private-rationale-123",
                                }
                            ]
                        },
                        "result": {"fills": [], "rejections": []},
                    }
                },
            }
            (data_dir / "ticks.jsonl").write_text(json.dumps(tick) + "\n", encoding="utf-8")

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")
            manifest = json.loads((out_dir / "manifest.json").read_text())

        public_blob = html + json.dumps(manifest, sort_keys=True)
        self.assertIn("score band", html)
        self.assertIn("coarse tick summary", html)
        self.assertIn("orders 1", html)
        self.assertIn("Backup", html)
        self.assertIn("Replay", html)
        for forbidden in (
            "private-position-key",
            "private-market-key",
            "private-question",
            "private-rationale-123",
            "raw-snapshot-secret",
            '"cash"',
            '"portfolio_value"',
            '"details"',
        ):
            self.assertNotIn(forbidden, public_blob)
        self.assertEqual(manifest["view"], "public_trade_terminal_v3")
        self.assertEqual(manifest["scores"][0]["score_band"], "up")

    def test_operator_output_is_redacted_but_keeps_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            operator_dir = root / "operator"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            dummy_host = ".".join(["192", "0", "2", "44"])
            dummy_bearer = "Bearer " + "abcdefghijklmnop"
            tick = {
                "tick_id": "operator",
                "ts": "2026-06-14T03:30:00+00:00",
                "snapshot_file": "/Users/example/.ssh/private_key",
                "remote_error": f"ssh failed against {dummy_host} with {dummy_bearer}",
                "agents": {
                    "superwing": {
                        "decision": {"orders": [{"rationale": "operator-detail-rationale"}]},
                        "result": {"fills": [], "rejections": []},
                    }
                },
                "scores": [],
            }
            (data_dir / "ticks.jsonl").write_text(json.dumps(tick) + "\n", encoding="utf-8")

            generate_dashboard.render(
                argparse.Namespace(
                    data_dir=str(data_dir),
                    env_file="",
                    output_dir=str(out_dir),
                    operator_output_dir=str(operator_dir),
                )
            )
            operator_json = (operator_dir / "operator.json").read_text(encoding="utf-8")

        self.assertIn("operator-detail-rationale", operator_json)
        self.assertIn("[redacted]", operator_json)
        self.assertNotIn(".ssh", operator_json)
        self.assertNotIn(dummy_host, operator_json)
        self.assertNotIn(dummy_bearer, operator_json)

    def test_status_report_includes_runtime_contract_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)

            report = aurum_status_report.build_report(data_dir, root, max_stale_seconds=180)

        self.assertEqual(report["completion_state"], "code-complete-only")
        for key in (
            "recorder",
            "manifest",
            "book_coverage",
            "orderable_market_count",
            "bot_registry",
            "backup",
            "replay",
            "risk_ledger",
        ):
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
