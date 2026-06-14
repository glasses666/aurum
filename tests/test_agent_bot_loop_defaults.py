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
import market_recorder


class HealthyRecorderFetcher:
    def __call__(self, url, timeout=12.0):
        if "gamma-api.polymarket.com" in url:
            return [
                {
                    "id": "btc",
                    "question": "Will Bitcoin close above 100k?",
                    "slug": "btc-100k",
                    "category": "Crypto",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.42", "0.58"]',
                    "clobTokenIds": '["tok_yes", "tok_no"]',
                    "volume": "5000",
                    "liquidity": "1000",
                }
            ]
        if "clob.polymarket.com/markets" in url:
            return {"markets": []}
        if "data-api.polymarket.com/trades" in url:
            return []
        if "clob.polymarket.com/book" in url:
            return {"bids": [["0.41", "100"]], "asks": [["0.43", "80"]]}
        raise AssertionError(url)


class AgentBotLoopDefaultTests(unittest.TestCase):
    def write_healthy_recorder(self, root: pathlib.Path) -> None:
        market_recorder.capture_once(
            root,
            fetcher=HealthyRecorderFetcher(),
            now=lambda: "2026-06-14T03:30:00+00:00",
            max_books=2,
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
        self.assertEqual(tick["effective_mode"], "hold_only")
        self.assertFalse(tick["applied"])
        self.assertIn("missing_bot_registry_manifest", tick["bot_script_manifest"]["errors"])

    def test_blank_env_mode_defaults_to_review_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_healthy_recorder(root)
            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999", "AURUM_DUEL_MODE": ""}, clear=True):
                tick = agent_bot_loop.run_mechanical_tick(self.args(root))

        self.assertEqual(tick["mode"], "review_only")
        self.assertEqual(tick["effective_mode"], "hold_only")
        self.assertFalse(tick["applied"])
        self.assertIn("missing_bot_registry_manifest", tick["bot_script_manifest"]["errors"])

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
