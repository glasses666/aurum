import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_duel
import agent_duel_tick


class LegacyAgentDuelTickTests(unittest.TestCase):
    def test_paper_apply_direct_fetch_path_fails_before_state_mutation_without_explicit_legacy_gate(self):
        market = {
            "market_id": "btc",
            "question": "Will Bitcoin close above 100k?",
            "volume": 5000,
            "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
        }
        args = types.SimpleNamespace(
            data_dir="",
            env_file="",
            mode="paper_apply",
            limit=1,
            min_volume=0,
            max_orders=2,
            mock_markets="",
            allow_proxy=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            args.data_dir = str(data_dir)
            decision = {
                "agent_id": "superwing",
                "orders": [{"market_id": "btc", "outcome": "Yes", "side": "buy", "notional": 10.0, "limit_price": 0.43}],
            }
            with mock.patch.dict(
                "os.environ",
                {
                    "AURUM_DEEPSEEK_ALLOW_PAPER_APPLY": "",
                    "AURUM_DEEPSEEK_OPERATOR_CONFIRM": "",
                    "AURUM_ALLOW_LEGACY_DIRECT_FETCH_PAPER_APPLY": "",
                },
                clear=False,
            ):
                with mock.patch.object(agent_duel, "fetch_markets", return_value=[market]):
                    with mock.patch.object(agent_duel, "superwing_decision", return_value=decision):
                        with self.assertRaisesRegex(agent_duel.DuelError, "legacy direct-fetch paper_apply disabled"):
                            agent_duel_tick.run_tick(args)

            self.assertFalse((data_dir / "state.json").exists())
            self.assertFalse((data_dir / "ticks.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
