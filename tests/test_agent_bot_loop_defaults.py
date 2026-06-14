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

import agent_bot_loop
import agent_duel


class AgentBotLoopDefaultTests(unittest.TestCase):
    def write_healthy_recorder(self, root: pathlib.Path) -> None:
        (root / "reports").mkdir(parents=True, exist_ok=True)
        (root / "normalized" / "polymarket").mkdir(parents=True, exist_ok=True)
        ts = "2026-06-14T03:30:00+00:00"
        (root / "reports" / "market_recorder_health.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "ts": ts,
                    "source": "polymarket_market_recorder_v0",
                    "sources": {
                        "gamma_markets": {"ok_frames": 1},
                        "clob_markets": {"ok_frames": 1},
                        "data_trades": {"ok_frames": 1},
                        "clob_book": {"ok_frames": 2, "requested_tokens": 2},
                    },
                    "book_coverage": {"requested_tokens": 2, "ok_tokens": 2, "orderable_tokens": 2},
                    "orderable_market_count": 1,
                    "manifest": {"ok": True, "frames": 4, "verified_rows": 4, "latest_sequence": 4, "verification_scope": "tail", "max_rows": 500, "frame_tail_rows": 2000},
                }
            ),
            encoding="utf-8",
        )
        (root / "normalized" / "polymarket" / "latest_markets.json").write_text(
            json.dumps(
                {
                    "ts": ts,
                    "source": "polymarket_market_recorder_v0",
                    "book_coverage": {"requested_tokens": 2, "ok_tokens": 2, "orderable_tokens": 2},
                    "orderable_market_count": 1,
                    "markets": [
                        {
                            "market_id": "btc",
                            "question": "Will Bitcoin close above 100k?",
                            "volume": 5000,
                            "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def args(self, root: pathlib.Path, *, mode=None):
        return types.SimpleNamespace(
            data_dir=str(root / "paper_duel"),
            env_file="",
            mode=mode,
            limit=1,
            min_volume=0,
            max_orders=2,
            mock_markets="",
            allow_proxy=False,
            dashboard_dir="",
        )

    def test_empty_env_defaults_to_review_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_healthy_recorder(root)
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999"}, clear=True):
                tick = agent_bot_loop.run_mechanical_tick(self.args(root))

        self.assertEqual(tick["mode"], "review_only")
        self.assertEqual(tick["effective_mode"], "review_only")
        self.assertFalse(tick["applied"])

    def test_blank_env_mode_defaults_to_review_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_healthy_recorder(root)
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999", "AURUM_DUEL_MODE": ""}, clear=True):
                tick = agent_bot_loop.run_mechanical_tick(self.args(root))

        self.assertEqual(tick["mode"], "review_only")
        self.assertEqual(tick["effective_mode"], "review_only")
        self.assertFalse(tick["applied"])

    def test_paper_apply_requires_explicit_mode_and_deepseek_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_healthy_recorder(root)
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999", "AURUM_DUEL_MODE": "paper_apply"}, clear=True):
                with self.assertRaises(agent_duel.DuelError):
                    agent_bot_loop.run_mechanical_tick(self.args(root))

    def test_paper_apply_auth_failure_does_not_partially_mutate_superwing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_healthy_recorder(root)
            data_dir = root / "paper_duel"
            before = agent_duel.init_state(data_dir, reset=True)
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999", "AURUM_DUEL_MODE": "paper_apply"}, clear=True):
                with self.assertRaises(agent_duel.DuelError):
                    agent_bot_loop.run_mechanical_tick(self.args(root))
            after = agent_duel.load_state(data_dir)

        self.assertEqual(after["accounts"]["superwing"]["cash"], before["accounts"]["superwing"]["cash"])
        self.assertEqual(after["accounts"]["superwing"].get("trades", []), [])
        self.assertEqual(after["accounts"]["superwing"].get("positions", {}), {})

    def test_fresh_paper_apply_auth_failure_writes_no_runtime_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_healthy_recorder(root)
            data_dir = root / "paper_duel"
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999", "AURUM_DUEL_MODE": "paper_apply"}, clear=True):
                with self.assertRaises(agent_duel.DuelError):
                    agent_bot_loop.run_mechanical_tick(self.args(root))

            self.assertFalse((data_dir / "state.json").exists())
            self.assertFalse((data_dir / "events.jsonl").exists())
            self.assertFalse((data_dir / "bot_scripts").exists())

    def test_cli_review_only_overrides_paper_apply_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_healthy_recorder(root)
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999", "AURUM_DUEL_MODE": "paper_apply"}, clear=True):
                tick = agent_bot_loop.run_mechanical_tick(self.args(root, mode="review_only"))

        self.assertEqual(tick["mode"], "review_only")
        self.assertFalse(tick["applied"])

    def test_run_bot_loop_shell_default_is_review_only(self):
        script = (ROOT / "scripts" / "run_bot_loop.sh").read_text(encoding="utf-8")
        self.assertIn('MODE="${AURUM_DUEL_MODE:-review_only}"', script)


if __name__ == "__main__":
    unittest.main()
