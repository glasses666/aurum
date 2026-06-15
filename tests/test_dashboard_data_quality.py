import argparse
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_duel
import aurum_status_report
import bot_scripts
import generate_dashboard
import quant_lanes
import strategy_rules
import strategy_review


class DashboardDataQualityTests(unittest.TestCase):
    def test_limited_jsonl_read_does_not_load_entire_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ticks.jsonl"
            path.write_text(
                "\n".join(json.dumps({"idx": idx, "payload": "x" * 1024}) for idx in range(5)) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(pathlib.Path, "read_text", side_effect=AssertionError("full file read")):
                rows = generate_dashboard.read_jsonl(path, limit=2)

        self.assertEqual([row["idx"] for row in rows], [3, 4])

    def test_risk_ledger_status_uses_bounded_tail_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            ledger_path = data_dir / "risk_ledger.jsonl"
            ledger_path.write_text(json.dumps({"idx": 1}) + "\n", encoding="utf-8")

            with mock.patch.object(generate_dashboard, "read_jsonl", return_value=[{"idx": 1}, {"idx": 2}]) as read_jsonl:
                status = generate_dashboard.risk_ledger_status(data_dir)

        read_jsonl.assert_called_once_with(ledger_path, limit=500)
        self.assertEqual(status["rows_sampled"], 2)
        self.assertEqual(status["read_scope"], "tail")

    def test_risk_ledger_status_does_not_label_tail_sample_as_total_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            ledger_path = data_dir / "risk_ledger.jsonl"
            ledger_path.write_text("\n".join(json.dumps({"idx": idx}) for idx in range(600)) + "\n", encoding="utf-8")

            status = generate_dashboard.risk_ledger_status(data_dir)

        self.assertEqual(status["rows_sampled"], 500)
        self.assertEqual(status["read_scope"], "tail")
        self.assertNotIn("rows", status)

    def test_strategy_rule_summary_versions_uses_bounded_tail_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            strategy_rules.ensure_default_rules(data_dir)
            version_log = strategy_rules.version_log_path(data_dir)
            version_log.write_text(
                "\n".join(json.dumps({"idx": idx, "action": "promote"}) for idx in range(20)) + "\n",
                encoding="utf-8",
            )
            original_read_text = pathlib.Path.read_text

            def guarded_read_text(path, *args, **kwargs):
                if pathlib.Path(path) == version_log:
                    raise AssertionError("full version log read")
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "read_text", guarded_read_text):
                summary = strategy_rules.summarize_rules(data_dir)

        self.assertEqual(len(summary["versions"]), 12)
        self.assertEqual(summary["versions"][0]["idx"], 8)
        self.assertEqual(summary["versions"][-1]["idx"], 19)

    def test_strategy_review_context_includes_replay_feedback_for_self_evolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            agent_duel.init_state(data_dir, reset=True)
            (data_dir / "reports").mkdir(parents=True)
            (data_dir / "reports" / "replay_summary.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "mode": "audit",
                        "risk_ledger_rows": 3,
                        "bot_registry_ok": True,
                        "findings": ["fee_churn_detected"],
                    }
                ),
                encoding="utf-8",
            )

            ctx = strategy_review.review_context(data_dir, limit_ticks=2)

        self.assertEqual(ctx["replay_feedback"]["status"], "ok")
        self.assertEqual(ctx["replay_feedback"]["risk_ledger_rows"], 3)
        self.assertIn("Use own ledger/fills/risk_events/replay outcomes", ctx["self_evolution_contract"])

    def test_strategy_review_agent_learning_context_is_peer_aggregate_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            state = agent_duel.init_state(data_dir, reset=True)
            own = state["accounts"]["deepseek"]
            own["trades"] = [{"market_id": "deep-own-market", "rationale": "deep own rationale", "fee": 0.01}]
            peer = state["accounts"]["superwing"]
            peer["trades"] = [{"market_id": "peer-secret-market", "rationale": "peer raw rationale", "fee": 0.02}]
            agent_duel.save_state(data_dir, state)

            shared_ctx = strategy_review.review_context(data_dir, limit_ticks=2)
            shared_text = json.dumps(shared_ctx["agent_learning_contexts"], ensure_ascii=False, sort_keys=True)
            isolated = strategy_review.agent_review_context(data_dir, "deepseek", limit_ticks=2)
            deep_ctx = isolated["agent_learning_context"]
            deep_text = json.dumps(deep_ctx, ensure_ascii=False, sort_keys=True)

        self.assertNotIn("deep-own-market", shared_text)
        self.assertNotIn("deep own rationale", shared_text)
        self.assertNotIn("peer-secret-market", shared_text)
        self.assertNotIn("peer raw rationale", shared_text)
        self.assertEqual(isolated["target_agent_id"], "deepseek")
        self.assertIn("deep-own-market", deep_text)
        self.assertIn("deep own rationale", deep_text)
        self.assertNotIn("peer-secret-market", deep_text)
        self.assertNotIn("peer raw rationale", deep_text)
        self.assertIn("peer_scoreboard", deep_ctx)
        self.assertNotIn("recent_trades", deep_ctx["peer_scoreboard"][0])

    def test_strategy_review_recent_tick_scores_are_public_aggregate_only(self):
        tick = {
            "tick_id": "tick-peer-leak",
            "ts": "2026-06-14T03:30:00+00:00",
            "mode": "paper_apply",
            "applied": True,
            "market_count": 1,
            "scores": [
                {
                    "agent_id": "superwing",
                    "rank": 1,
                    "score": 4.2,
                    "roi": 0.01,
                    "trade_count": 2,
                    "cash": 1490.0,
                    "fees_paid": 0.2,
                    "details": [{"market_id": "peer-secret-position", "shares": 12.0}],
                    "positions": {"peer-secret-position": {"shares": 12.0}},
                }
            ],
            "agents": {},
        }

        compact = strategy_review.compact_tick(tick)
        blob = json.dumps(compact, ensure_ascii=False, sort_keys=True)

        self.assertEqual(compact["scores"][0]["agent_id"], "superwing")
        self.assertEqual(compact["scores"][0]["rank"], 1)
        self.assertIn("trade_count", compact["scores"][0])
        for private_field in ("cash", "fees_paid", "details", "positions"):
            self.assertNotIn(private_field, compact["scores"][0])
        self.assertNotIn("peer-secret-position", blob)

    def test_run_review_calls_model_once_per_agent_with_isolated_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            state = agent_duel.init_state(data_dir, reset=True)
            state["accounts"]["deepseek"]["trades"] = [
                {"market_id": "deep-own-market", "rationale": "deep own rationale", "fee": 0.01}
            ]
            state["accounts"]["superwing"]["trades"] = [
                {"market_id": "peer-secret-market", "rationale": "peer raw rationale", "fee": 0.02}
            ]
            agent_duel.save_state(data_dir, state)
            strategy_rules.ensure_default_rules(data_dir)
            superwing_rules = strategy_rules.load_superwing_rules(data_dir)
            superwing_rules["selection"] = "SUPERWING-PRIVATE-STRATEGY-INTERNAL paper buy hold"
            strategy_rules.superwing_rules_path(data_dir).write_text(
                json.dumps(superwing_rules, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            strategy_rules.deepseek_rules_path(data_dir).write_text(
                "# DeepSeek paper rules\n- DEEPSEEK-PRIVATE-STRATEGY-INTERNAL: paper buy sell hold only.\n",
                encoding="utf-8",
            )
            bot_scripts.ensure_default_bot_scripts(data_dir)
            seen_contexts = {}

            def fake_review_model(ctx):
                agent = ctx["target_agent_id"]
                seen_contexts[agent] = json.dumps(ctx, ensure_ascii=False, sort_keys=True)
                if agent == "superwing":
                    return {
                        "review_outcome": "PROPOSE_UPDATE",
                        "summary": "SuperWing isolated review",
                        "findings": ["superwing-own-ledger-only"],
                        "proposal_metrics": {"schema_valid": True, "replay_ok": True, "holdout_ok": True},
                        "superwing_rules": strategy_rules.load_superwing_rules(data_dir),
                        "superwing_rationale": "Keep conservative paper-only settings after reviewing own ledger.",
                        "bot_scripts": {"superwing": bot_scripts.load_bot_script(data_dir, "superwing")},
                        "risk_notes": ["paper-only"],
                        "public_dashboard_note": "SuperWing reviewed in an isolated lane.",
                        "review_model": "mock-superwing",
                    }
                return {
                    "review_outcome": "PROPOSE_UPDATE",
                    "summary": "DeepSeek isolated review",
                    "findings": ["deepseek-own-ledger-only"],
                    "proposal_metrics": {"schema_valid": True, "replay_ok": True, "holdout_ok": True},
                    "deepseek_rules_md": strategy_rules.load_deepseek_rules(data_dir),
                    "deepseek_rationale": "Keep paper-only buy/sell/hold rules after reviewing own ledger.",
                    "bot_scripts": {"deepseek": bot_scripts.load_bot_script(data_dir, "deepseek")},
                    "risk_notes": ["paper-only"],
                    "public_dashboard_note": "DeepSeek reviewed in an isolated lane.",
                    "review_model": "mock-deepseek",
                }

            with mock.patch.object(strategy_review, "call_review_model", side_effect=fake_review_model):
                record = strategy_review.run_review(
                    argparse.Namespace(
                        data_dir=str(data_dir),
                        env_file="",
                        dashboard_dir=str(out_dir),
                        operator_dashboard_dir="",
                        limit_ticks=2,
                        auto_promote=False,
                        no_promote=True,
                    )
                )

        self.assertEqual(set(seen_contexts), {"superwing", "deepseek"})
        self.assertIn("deep-own-market", seen_contexts["deepseek"])
        self.assertNotIn("peer-secret-market", seen_contexts["deepseek"])
        self.assertIn("DEEPSEEK-PRIVATE-STRATEGY-INTERNAL", seen_contexts["deepseek"])
        self.assertNotIn("SUPERWING-PRIVATE-STRATEGY-INTERNAL", seen_contexts["deepseek"])
        self.assertIn("peer-secret-market", seen_contexts["superwing"])
        self.assertNotIn("deep-own-market", seen_contexts["superwing"])
        self.assertIn("SUPERWING-PRIVATE-STRATEGY-INTERNAL", seen_contexts["superwing"])
        self.assertNotIn("DEEPSEEK-PRIVATE-STRATEGY-INTERNAL", seen_contexts["superwing"])
        self.assertFalse(record["auto_promote"])
        self.assertIn("per-agent-isolated", record["review_model"])

    def test_run_review_never_promotes_outputs_from_fallback_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            strategy_rules.ensure_default_rules(data_dir)
            bot_scripts.ensure_default_bot_scripts(data_dir)

            def fallback_review(ctx):
                agent = ctx["target_agent_id"]
                review = {
                    "review_outcome": "PROPOSE_UPDATE",
                    "summary": f"Fallback review for {agent}",
                    "findings": ["fallback-model-used"],
                    "proposal_metrics": {"schema_valid": True, "replay_ok": True, "holdout_ok": True},
                    "bot_scripts": {agent: bot_scripts.load_bot_script(data_dir, agent)},
                    "risk_notes": ["fallback should not promote"],
                    "public_dashboard_note": "Fallback model used; keep current rules.",
                    "review_model": "deepseek-v4-flash",
                    "primary_review_model": "deepseek-v4-pro",
                    "review_model_fallback_used": True,
                    "review_model_usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                }
                if agent == "superwing":
                    review["superwing_rules"] = strategy_rules.load_superwing_rules(data_dir)
                    review["superwing_rationale"] = "Fallback model must not promote SuperWing rules."
                else:
                    review["deepseek_rules_md"] = strategy_rules.load_deepseek_rules(data_dir)
                    review["deepseek_rationale"] = "Fallback model must not promote DeepSeek rules."
                return review

            with mock.patch.object(strategy_review, "call_review_model", side_effect=fallback_review):
                record = strategy_review.run_review(
                    argparse.Namespace(
                        data_dir=str(data_dir),
                        env_file="",
                        dashboard_dir=str(out_dir),
                        operator_dashboard_dir="",
                        limit_ticks=2,
                        auto_promote=True,
                        no_promote=False,
                    )
                )

            self.assertTrue(record["model_fallback_used"])
            self.assertEqual(record["review_status"], "fallback_no_promote")
            self.assertFalse(record["auto_promote"])
            self.assertEqual(record["promoted"], {})
            self.assertEqual(record["review_model_usage"]["agents"]["superwing"]["total_tokens"], 14)
            self.assertEqual(record["review_model_usage"]["aggregate"]["total_tokens"], 28)
            for agent in agent_duel.AGENTS:
                agent_record = data_dir / "strategy_reviews" / "agents" / agent / f"{record['review_id']}.json"
                self.assertTrue(agent_record.exists())
                payload = json.loads(agent_record.read_text(encoding="utf-8"))
                self.assertEqual(payload["agent_id"], agent)
                self.assertEqual(payload["learning_scope"], "target_agent_raw_ledger_only")
                self.assertTrue(payload["model_fallback_used"])

    def test_review_keep_current_strategy_writes_no_strategy_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            strategy_rules.ensure_default_rules(data_dir)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            original_superwing = strategy_rules.superwing_rules_path(data_dir).read_text(encoding="utf-8")
            original_deepseek = strategy_rules.deepseek_rules_path(data_dir).read_text(encoding="utf-8")

            def keep_review(ctx):
                return {
                    "review_outcome": "KEEP_CURRENT_STRATEGY",
                    "summary": "Keep current strategy; no executable rewrite.",
                    "findings": ["current strategy remains within bounds"],
                    "risk_notes": ["paper-only"],
                    "public_dashboard_note": "keep current strategy",
                    "review_model": "mock-keep",
                }

            with mock.patch.object(strategy_review, "call_review_model", side_effect=keep_review):
                record = strategy_review.run_review(
                    argparse.Namespace(
                        data_dir=str(data_dir),
                        env_file="",
                        dashboard_dir=str(out_dir),
                        operator_dashboard_dir="",
                        limit_ticks=2,
                        auto_promote=True,
                        no_promote=False,
                    )
                )
            final_superwing = strategy_rules.superwing_rules_path(data_dir).read_text(encoding="utf-8")
            final_deepseek = strategy_rules.deepseek_rules_path(data_dir).read_text(encoding="utf-8")

        self.assertEqual(record["review_status"], "model_ok")
        self.assertEqual(record["review_outcomes"], {"superwing": "KEEP_CURRENT_STRATEGY", "deepseek": "KEEP_CURRENT_STRATEGY"})
        self.assertFalse(record["auto_promote"])
        self.assertEqual(record["promoted"], {})
        self.assertEqual(final_superwing, original_superwing)
        self.assertEqual(final_deepseek, original_deepseek)
        self.assertEqual(record["proposed"]["superwing_protocol"], "KEEP_CURRENT_STRATEGY")
        self.assertEqual(record["proposed"]["deepseek_protocol"], "KEEP_CURRENT_STRATEGY")

    def test_invalid_review_protocol_falls_back_without_promote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)

            def invalid_review(ctx):
                return {
                    "summary": "Missing required outcome",
                    "findings": [],
                    "review_model": "mock-invalid",
                }

            with mock.patch.object(strategy_review, "call_review_model", side_effect=invalid_review):
                record = strategy_review.run_review(
                    argparse.Namespace(
                        data_dir=str(data_dir),
                        env_file="",
                        dashboard_dir=str(out_dir),
                        operator_dashboard_dir="",
                        limit_ticks=2,
                        auto_promote=True,
                        no_promote=False,
                    )
                )
            agent_records = {
                agent: json.loads((data_dir / "strategy_reviews" / "agents" / agent / f"{record['review_id']}.json").read_text(encoding="utf-8"))
                for agent in agent_duel.AGENTS
            }

        self.assertEqual(record["review_status"], "fallback_no_promote")
        self.assertFalse(record["auto_promote"])
        self.assertEqual(record["promoted"], {})
        self.assertEqual(record["review_model"], "fallback_no_promote")
        for agent in agent_duel.AGENTS:
            self.assertEqual(agent_records[agent]["review_protocol_error"], "missing_review_outcome")

    def test_review_protocol_rejects_legacy_outcome_missing_metrics_and_partial_script(self):
        self.assertEqual(
            strategy_review.normalize_review_protocol({"outcome": "KEEP_CURRENT_STRATEGY"}, "superwing")["error"],
            "unknown_review_field:outcome",
        )
        missing_metrics = strategy_review.normalize_review_protocol(
            {
                "review_outcome": "PROPOSE_UPDATE",
                "summary": "proposal without local metric claims",
                "findings": [],
                "superwing_rules": strategy_rules.DEFAULT_SUPERWING_RULES,
            },
            "superwing",
        )
        self.assertEqual(missing_metrics["error"], "propose_update_missing_proposal_metrics")
        partial_script = strategy_review.normalize_review_protocol(
            {
                "review_outcome": "PROPOSE_UPDATE",
                "summary": "partial script body",
                "findings": [],
                "proposal_metrics": {},
                "bot_scripts": {"superwing": {"buy_when": {"price_min": 0.2}}},
            },
            "superwing",
        )
        self.assertTrue(partial_script["error"].startswith("propose_update_bot_script_invalid:"))

    def test_request_hold_only_freezes_lane_without_strategy_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)

            def mixed_review(ctx):
                agent = ctx["target_agent_id"]
                if agent == "deepseek":
                    return {
                        "review_outcome": "REQUEST_HOLD_ONLY",
                        "summary": "Freeze new entries after anomalies.",
                        "findings": ["risk anomaly"],
                        "risk_notes": ["hold-only"],
                        "review_model": "mock-hold",
                    }
                return {
                    "review_outcome": "KEEP_CURRENT_STRATEGY",
                    "summary": "Keep SuperWing.",
                    "findings": [],
                    "risk_notes": ["paper-only"],
                    "review_model": "mock-keep",
                }

            with mock.patch.object(strategy_review, "call_review_model", side_effect=mixed_review):
                record = strategy_review.run_review(
                    argparse.Namespace(
                        data_dir=str(data_dir),
                        env_file="",
                        dashboard_dir=str(out_dir),
                        operator_dashboard_dir="",
                        limit_ticks=2,
                        auto_promote=True,
                        no_promote=False,
                    )
                )

            control = quant_lanes.load_lane_control(data_dir, "deepseek")

        self.assertEqual(record["review_status"], "model_ok")
        self.assertEqual(record["review_outcomes"]["deepseek"], "REQUEST_HOLD_ONLY")
        self.assertEqual(control["status"], "hold_only")
        self.assertIn("deepseek", record["lane_controls"])
        self.assertNotIn("deepseek_bot_script", record["proposed"])

    def test_model_claimed_promotion_metrics_cannot_clear_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)

            def fake_metrics_review(ctx):
                agent = ctx["target_agent_id"]
                current = bot_scripts.load_bot_script(data_dir, agent)
                promoted = bot_scripts.merge_script_update(current, {})
                promoted["review"] = {
                    "status": "promoted",
                    "reviewed_by": "unit",
                    "reviewed_at": "2026-06-14T03:30:00+00:00",
                    "promotion_id": "unit",
                }
                if agent == "deepseek":
                    promoted["hold_only"] = False
                    promoted["allowed_sides"] = ["buy", "sell"]
                    promoted["max_orders_per_tick"] = 1
                    promoted["buy_when"] = {**promoted["buy_when"], "enabled": True, "max_notional": 10.0}
                    promoted["sell_when"] = {**promoted["sell_when"], "enabled": True}
                return {
                    "review_outcome": "PROPOSE_UPDATE",
                    "summary": "Model claims this should promote.",
                    "findings": ["model-claimed-metrics"],
                    "superwing_rules": strategy_rules.load_superwing_rules(data_dir),
                    "deepseek_rules_md": strategy_rules.load_deepseek_rules(data_dir),
                    "bot_scripts": {agent: promoted},
                    "proposal_metrics": {
                        "schema_valid": True,
                        "replay_ok": True,
                        "holdout_ok": True,
                        "after_fee_roi": 999.0,
                        "max_drawdown": 0.0,
                        "trade_count": 100,
                        "churn": 0.0,
                        "exposure_concentration": 0.0,
                    },
                    "risk_notes": ["paper-only"],
                    "review_model": "mock-claims",
                }

            with mock.patch.object(strategy_review, "call_review_model", side_effect=fake_metrics_review):
                record = strategy_review.run_review(
                    argparse.Namespace(
                        data_dir=str(data_dir),
                        env_file="",
                        dashboard_dir=str(out_dir),
                        operator_dashboard_dir="",
                        limit_ticks=2,
                        auto_promote=True,
                        no_promote=False,
                    )
                )

        self.assertEqual(record["promoted"], {})
        self.assertFalse(record["auto_promote"])
        for agent in agent_duel.AGENTS:
            gate = record["promotion_gates"][agent]
            self.assertEqual(gate["status"], "rejected")
            self.assertTrue(gate["model_claimed_metrics_ignored"])
            self.assertIn("schema_not_valid", gate["reasons"])
            self.assertIn("replay_not_ok", gate["reasons"])

    def test_call_review_model_records_api_usage_metadata_not_model_claim(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "summary": "safe review",
                                            "findings": [],
                                            "review_model_usage": {"total_tokens": 999999},
                                        }
                                    )
                                }
                            }
                        ],
                    }
                ).encode("utf-8")

        class FakeOpener:
            def open(self, req, timeout):
                return FakeResponse()

        with mock.patch.dict(
            strategy_review.os.environ,
            {
                "DEEPSEEK_API_KEY": "unit-test-key",
                "AURUM_REVIEW_MODEL": "deepseek-v4-pro",
                "DEEPSEEK_MODEL": "deepseek-v4-flash",
                "AURUM_REVIEW_THINKING": "disabled",
            },
            clear=False,
        ), mock.patch.object(strategy_review.duel, "no_proxy_opener", return_value=FakeOpener()):
            review = strategy_review.call_review_model({"target_agent_id": "superwing"})

        self.assertEqual(review["review_model_usage"], {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})
        self.assertNotEqual(review["review_model_usage"].get("total_tokens"), 999999)

    def test_redact_value_redacts_sensitive_dictionary_keys(self):
        private_label = "aurum-" + "testhost-01"
        safe = generate_dashboard.redact_value({private_label: {"ok": True}})

        self.assertNotIn(private_label, safe)
        self.assertIn("[redacted]", safe)

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
        self.assertEqual(manifest["view"], "public_trade_terminal_v4_chinese_first")
        self.assertEqual(manifest["scores"][0]["score_band"], "up")

    def test_public_dashboard_is_chinese_first_and_keeps_stable_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        for label in ("数据质量", "运行时 Runtime", "模型 lanes", "持仓 Positions", "活动日志 Activity Log", "BTC Yes 概率曲线"):
            self.assertIn(label, html)
        for marker in ("code-complete-only", "paper-only", "score band", "coarse tick summary"):
            self.assertIn(marker, html)

    def test_btc_chart_summary_marks_variable_and_flat_series(self):
        variable = generate_dashboard.btc_chart_summary(
            [
                {"ts": "2026-06-14T03:30:00+00:00", "value": 0.42},
                {"ts": "2026-06-14T03:31:00+00:00", "value": 0.47},
            ]
        )
        flat = generate_dashboard.btc_chart_summary(
            [
                {"ts": "2026-06-14T03:30:00+00:00", "value": 0.42},
                {"ts": "2026-06-14T03:31:00+00:00", "value": 0.42},
            ]
        )

        self.assertFalse(variable["is_flat"])
        self.assertEqual(variable["status"], "variable")
        self.assertTrue(flat["is_flat"])
        self.assertEqual(flat["status"], "flat")
        self.assertIn("平坦", flat["status_label"])

    def test_public_dashboard_serializes_nonflat_btc_series_as_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            snapshot_dir = data_dir / "snapshots"
            snapshot_dir.mkdir(parents=True)
            agent_duel.init_state(data_dir, reset=True)
            for idx, price in enumerate((0.42, 0.46), 1):
                (snapshot_dir / f"s{idx}.json").write_text(
                    json.dumps(
                        {
                            "snapshot_id": f"s{idx}",
                            "ts": f"2026-06-14T03:3{idx}:00+00:00",
                            "markets": [
                                {
                                    "market_id": "btc-yes",
                                    "question": "Will Bitcoin close above 100k?",
                                    "volume": 1000 + idx,
                                    "outcomes": [{"name": "Yes", "price": price}, {"name": "No", "price": 1 - price}],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertFalse(manifest["btc_chart"]["is_flat"])
        self.assertEqual(manifest["btc_chart"]["status"], "variable")
        self.assertIn("真实波动 variable", html)

    def test_snapshot_records_ignore_tick_paths_outside_snapshot_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            data_dir.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text(
                json.dumps({"snapshot_id": "outside", "ts": "2026-06-14T03:30:00+00:00", "markets": [{"market_id": "leak"}]}),
                encoding="utf-8",
            )
            ticks = [{"tick_id": "tampered", "snapshot_file": str(outside)}]

            rows = generate_dashboard.snapshot_records(data_dir, ticks)

        self.assertEqual(rows, [])

    def test_snapshot_records_ignore_symlinked_snapshots_outside_snapshot_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            snapshot_dir = data_dir / "snapshots"
            snapshot_dir.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text(
                json.dumps({"snapshot_id": "outside", "ts": "2026-06-14T03:30:00+00:00", "markets": [{"market_id": "leak"}]}),
                encoding="utf-8",
            )
            (snapshot_dir / "linked.json").symlink_to(outside)

            rows = generate_dashboard.snapshot_records(data_dir, [])

        self.assertEqual(rows, [])

    def test_snapshot_records_ignore_snapshot_root_symlink_outside_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            data_dir.mkdir(parents=True)
            outside_dir = root / "external-snapshots"
            outside_dir.mkdir()
            (outside_dir / "external.json").write_text(
                json.dumps({"snapshot_id": "external", "ts": "2026-06-14T03:30:00+00:00", "markets": [{"market_id": "leak"}]}),
                encoding="utf-8",
            )
            (data_dir / "snapshots").symlink_to(outside_dir, target_is_directory=True)

            rows = generate_dashboard.snapshot_records(data_dir, [])

        self.assertEqual(rows, [])

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

    def test_public_dashboard_does_not_plot_operator_trade_pips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            tick = {
                "tick_id": "fill-pip",
                "ts": "2026-06-14T03:30:00+00:00",
                "mode": "paper_apply",
                "scores": [{"agent_id": "superwing", "roi": 0.01}],
                "agents": {
                    "superwing": {
                        "decision": {"orders": [{"side": "buy", "outcome": "Yes", "limit_price": 0.4567}]},
                        "result": {
                            "fills": [
                                {
                                    "side": "buy",
                                    "outcome": "Yes",
                                    "fill_price": 0.4567,
                                    "notional": 9.5,
                                    "question": "operator-only exact fill question",
                                    "rationale": "operator-only rationale",
                                }
                            ],
                            "rejections": [],
                        },
                    }
                },
            }
            (data_dir / "ticks.jsonl").write_text(json.dumps(tick) + "\n", encoding="utf-8")

            public_path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = public_path.read_text(encoding="utf-8")

        self.assertNotIn('<circle class="trade-pip"', html)
        self.assertNotIn("0.457", html)
        self.assertNotIn("operator-only exact fill question", html)
        self.assertNotIn("operator-only rationale", html)

    def test_runtime_complete_rejects_inconsistent_gate_flags(self):
        healthy_parts = (
            {"ok": True},
            {"ok": True},
            {"ok": True},
            {"ok": True},
            {"ok": True, "rows_sampled": 1},
        )
        self.assertFalse(
            generate_dashboard.runtime_is_complete(
                {"decision": "TRADE_ALLOWED", "ok": True, "trade_allowed": False, "hold_only": True, "reason_codes": []},
                *healthy_parts,
            )
        )
        self.assertFalse(
            generate_dashboard.runtime_is_complete(
                {"decision": "TRADE_ALLOWED", "ok": True, "trade_allowed": True, "reason_codes": ["recorder_stale"]},
                *healthy_parts,
            )
        )
        self.assertFalse(
            generate_dashboard.runtime_is_complete(
                {"decision": "TRADE_ALLOWED", "ok": True, "trade_allowed": True, "hold_only": False, "stop_service": False, "reason_codes": "recorder_stale"},
                *healthy_parts,
            )
        )
        self.assertFalse(
            generate_dashboard.runtime_is_complete(
                {"decision": "TRADE_ALLOWED", "ok": True, "trade_allowed": True, "hold_only": False, "stop_service": False, "reason_codes": None},
                *healthy_parts,
            )
        )
        self.assertFalse(
            generate_dashboard.runtime_is_complete(
                {"decision": "TRADE_ALLOWED", "ok": True, "trade_allowed": True, "hold_only": False, "stop_service": False, "reason_codes": []},
                {"ok": True},
                {"ok": True},
                {"ok": True},
                {"ok": True},
                {"ok": True, "rows_sampled": "abc"},
            )
        )

    def test_public_runtime_status_fails_closed_on_malformed_gate_reason_codes(self):
        latest_tick = {
            "tick_id": "malformed-reasons",
            "data_quality_gate": {
                "decision": "TRADE_ALLOWED",
                "ok": True,
                "trade_allowed": True,
                "hold_only": False,
                "stop_service": False,
                "reason_codes": "recorder_stale",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            data_dir.mkdir(parents=True)
            with mock.patch.object(generate_dashboard, "public_recorder_summary", return_value={"ok": True}), mock.patch.object(
                generate_dashboard, "public_bot_registry_summary", return_value={"ok": True}
            ), mock.patch.object(generate_dashboard, "backup_status", return_value={"ok": True}), mock.patch.object(
                generate_dashboard, "replay_status", return_value={"ok": True}
            ), mock.patch.object(
                generate_dashboard, "risk_ledger_status", return_value={"ok": True, "rows_sampled": 1}
            ):
                runtime = generate_dashboard.public_runtime_status(data_dir, latest_tick)

        self.assertEqual(runtime["completion_state"], "code-complete-only")
        self.assertIn("invalid_reason_codes", runtime["data_quality_gate"]["reason_codes"])

    def test_backup_and_replay_status_require_boolean_true_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            recorder_dir = root / "recorder"
            reports_dir = recorder_dir / "reports"
            reports_dir.mkdir(parents=True)
            (reports_dir / "aurum_stability_backup_status.json").write_text(
                json.dumps({"ok": "false", "artifact_count": 3, "contains_manifest": True}),
                encoding="utf-8",
            )
            (reports_dir / "replay_summary.json").write_text(
                json.dumps({"ok": "false", "mode": "tampered"}),
                encoding="utf-8",
            )

            backup = generate_dashboard.backup_status(data_dir, recorder_dir=recorder_dir)
            replay = generate_dashboard.replay_status(data_dir, recorder_dir=recorder_dir)

        self.assertIs(backup["ok"], False)
        self.assertEqual(backup["status"], "check")
        self.assertIs(replay["ok"], False)
        self.assertEqual(replay["status"], "check")

    def test_status_report_evaluates_gate_from_explicit_recorder_dir_not_stale_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            recorder_dir = root / "recorder"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            (data_dir / "ticks.jsonl").write_text(
                json.dumps({"tick_id": "stale-ok", "data_quality_gate": {"decision": "TRADE_ALLOWED", "trade_allowed": True, "reason_codes": []}}) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                aurum_status_report.data_quality_gate,
                "evaluate_data_quality_gate",
                return_value={"decision": "HOLD_ONLY", "trade_allowed": False, "hold_only": True, "reason_codes": ["recorder_missing"]},
            ) as gate_mock:
                report = aurum_status_report.build_report(data_dir, recorder_dir, max_stale_seconds=180)

        self.assertEqual(pathlib.Path(gate_mock.call_args.args[0]), recorder_dir)
        self.assertEqual(report["data_quality_gate"]["decision"], "HOLD_ONLY")
        self.assertEqual(report["completion_state"], "code-complete-only")

    def test_redaction_hides_private_host_labels_but_keeps_book_coverage_counts(self):
        private_label = "aurum-" + "testhost-01"

        redacted = generate_dashboard.redact_value(
            {
                "message": f"probe failed on {private_label}",
                "book_coverage": {"requested_tokens": 2, "ok_tokens": 1, "orderable_tokens": 1},
                "token_id": "secret-token-id-like-value",
            }
        )

        blob = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(private_label, blob)
        self.assertIn("[redacted]", blob)
        self.assertEqual(redacted["book_coverage"]["requested_tokens"], 2)
        self.assertEqual(redacted["book_coverage"]["ok_tokens"], 1)
        self.assertEqual(redacted["book_coverage"]["orderable_tokens"], 1)
        self.assertEqual(redacted["token_id"], "[redacted]")

    def test_status_report_keeps_public_book_coverage_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            (root / "reports").mkdir(parents=True)
            (root / "reports" / "market_recorder_health.json").write_text(
                json.dumps(
                    {
                        "ok": False,
                        "ts": "2026-06-14T03:30:00+00:00",
                        "book_coverage": {"requested_tokens": 2, "ok_tokens": 1, "orderable_tokens": 1},
                        "sources": {},
                        "manifest": {"ok": False},
                    }
                ),
                encoding="utf-8",
            )

            report = aurum_status_report.build_report(data_dir, root, max_stale_seconds=999999999)

        self.assertEqual(report["book_coverage"]["requested_tokens"], 2)
        self.assertEqual(report["book_coverage"]["ok_tokens"], 1)

    def test_public_dashboard_redacts_gate_and_manifest_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            private_label = "aurum-" + "testhost-01"
            bearer = "Bearer " + "abcdefghijklmnop"
            ssh_path = "/Users/example/" + ".ssh/aurum_key"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            (data_dir / "ticks.jsonl").write_text(
                json.dumps(
                    {
                        "tick_id": "sensitive-gate",
                        "ts": "2026-06-14T03:30:00+00:00",
                        "mode": "paper_apply",
                        "loop_interval_sec": 15,
                        "market_source": {"source": f"{private_label} recorder"},
                        "data_quality_gate": {
                            "decision": "HOLD_ONLY",
                            "reason_codes": [f"remote {private_label} failed with {bearer} at {ssh_path}"],
                            "book_coverage": {
                                "requested_tokens": 2,
                                "ok_tokens": 1,
                                "orderable_tokens": 1,
                                "token_id": "secret-token-id-like-value",
                            },
                            "universe": f"bitcoin via {private_label}",
                        },
                        "scores": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            public_blob = path.read_text(encoding="utf-8") + (out_dir / "manifest.json").read_text(encoding="utf-8")
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertNotIn(private_label, public_blob)
        self.assertNotIn(bearer, public_blob)
        self.assertNotIn(".ssh", public_blob)
        self.assertNotIn("secret-token-id-like-value", public_blob)
        self.assertEqual(manifest["runtime"]["data_quality_gate"]["book_coverage"]["requested_tokens"], 2)
        self.assertEqual(manifest["runtime"]["data_quality_gate"]["book_coverage"]["ok_tokens"], 1)

    def test_public_html_redacts_env_market_question_and_invalid_count_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            env_file = root / ".env"
            private_label = "aurum-" + "testhost-01"
            env_file.write_text(f"AURUM_DUEL_UNIVERSE=bitcoin via {private_label}\n", encoding="utf-8")
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            snapshot_dir = data_dir / "snapshots"
            snapshot_dir.mkdir(parents=True)
            (snapshot_dir / "sensitive.json").write_text(
                json.dumps(
                    {
                        "snapshot_id": "sensitive",
                        "ts": "2026-06-14T03:30:00+00:00",
                        "markets": [
                            {
                                "market_id": "btc-sensitive",
                                "question": f"Will Bitcoin route through {private_label}?",
                                "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "ticks.jsonl").write_text(
                json.dumps(
                    {
                        "tick_id": "invalid-counts",
                        "ts": "2026-06-14T03:30:00+00:00",
                        "mode": "paper_apply",
                        "loop_interval_sec": 15,
                        "market_source": {"source": "polymarket_market_recorder_v0"},
                        "data_quality_gate": {
                            "decision": "HOLD_ONLY",
                            "reason_codes": [],
                            "book_coverage": {"requested_tokens": private_label, "ok_tokens": 1},
                            "orderable_market_count": private_label,
                        },
                        "scores": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file=str(env_file), output_dir=str(out_dir)))
            public_blob = path.read_text(encoding="utf-8") + (out_dir / "manifest.json").read_text(encoding="utf-8")

        self.assertNotIn(private_label, public_blob)
        self.assertIn("[redacted]", public_blob)

    def test_public_rule_panel_uses_metadata_not_raw_deepseek_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            raw_rule = "RAW STRATEGY PROMPT SECRET\n" + ("x" * 2000) + "\nTAIL UNIQUE RULE"
            expected_hash = hashlib.sha256(raw_rule.encode("utf-8")).hexdigest()
            agent_duel.init_state(data_dir, reset=True)
            strategy_rules.ensure_default_rules(data_dir)
            strategy_rules.deepseek_rules_path(data_dir).write_text(raw_rule, encoding="utf-8")

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        self.assertNotIn(raw_rule, html)
        self.assertIn("deepseek_rules_sha256", html)
        self.assertIn(expected_hash, html)

    def test_public_html_redacts_review_and_runtime_scalar_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            private_label = "aurum-" + "testhost-01"
            private_path = "/Users/example/" + ".ssh" + "/aurum_key"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            review_dir = data_dir / "strategy_reviews"
            review_dir.mkdir(parents=True)
            (review_dir / "review.json").write_text(
                json.dumps({"review_id": "review-1", "summary": f"checked {private_label} at {private_path}"}),
                encoding="utf-8",
            )
            (data_dir / "ticks.jsonl").write_text(
                json.dumps(
                    {
                        "tick_id": "runtime-sensitive",
                        "ts": "2026-06-14T03:30:00+00:00",
                        "mode": "paper_apply",
                        "loop_interval_sec": 15,
                        "market_source": {"source": "polymarket_market_recorder_v0"},
                        "data_quality_gate": {
                            "decision": "HOLD_ONLY",
                            "reason_codes": [],
                            "recorder_age_seconds": private_label,
                            "manifest_verification_scope": private_label,
                            "manifest_verification_max_rows": private_label,
                            "manifest_verification_verified_rows": private_label,
                            "book_coverage": {"requested_tokens": 2, "ok_tokens": 1},
                            "orderable_market_count": 1,
                        },
                        "scores": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            recorder_health = {
                "ok": False,
                "age_seconds": private_label,
                "errors": [f"probe failed at {private_path}"],
                "last_capture": {
                    "market_count": private_label,
                    "book_coverage": {"requested_tokens": private_label, "ok_tokens": 1},
                    "sources": {"clob_book": {"ok_frames": private_label}},
                    "manifest": {"ok": True, "verification_scope": private_label, "max_rows": private_label, "verified_rows": private_label},
                    "orderable_market_count": private_label,
                },
            }

            with mock.patch.object(generate_dashboard.market_recorder, "recorder_health", return_value=recorder_health):
                path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            public_blob = path.read_text(encoding="utf-8") + (out_dir / "manifest.json").read_text(encoding="utf-8")

        self.assertNotIn(private_label, public_blob)
        self.assertNotIn(private_path, public_blob)
        self.assertNotIn(".ssh", public_blob)
        self.assertIn("[redacted]", public_blob)

    def test_public_html_and_manifest_redact_tick_env_and_key_sinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            env_file = root / "aurum.env"
            private_label = "aurum-" + "testhost-02"
            env_file.write_text(
                f"AURUM_DUEL_MODE={private_label}\nAURUM_FIRST_CONTEST_DAYS={private_label}\n",
                encoding="utf-8",
            )
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            (data_dir / "ticks.jsonl").write_text(
                json.dumps(
                    {
                        "tick_id": private_label,
                        "ts": "2026-06-14T03:30:00+00:00",
                        "mode": private_label,
                        "effective_mode": private_label,
                        "applied": False,
                        private_label: {"nested": True},
                        "data_quality_gate": {"decision": "HOLD_ONLY", "reason_codes": [private_label]},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file=str(env_file), output_dir=str(out_dir)))
            public_blob = path.read_text(encoding="utf-8") + (out_dir / "manifest.json").read_text(encoding="utf-8")

        self.assertNotIn(private_label, public_blob)
        self.assertIn("[redacted]", public_blob)

    def test_operator_output_dir_cannot_overlap_public_dashboard_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)

            with self.assertRaises(ValueError):
                generate_dashboard.render(
                    argparse.Namespace(
                        data_dir=str(data_dir),
                        env_file="",
                        output_dir=str(out_dir),
                        operator_output_dir=str(out_dir),
                    )
                )
            with self.assertRaises(ValueError):
                generate_dashboard.render(
                    argparse.Namespace(
                        data_dir=str(data_dir),
                        env_file="",
                        output_dir=str(out_dir),
                        operator_output_dir=str(out_dir / "operator"),
                    )
                )

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
            "competition",
            "scoreboard",
            "victory",
        ):
            self.assertIn(key, report)
        self.assertEqual(report["competition"]["objective"], "rank_1_within_stability_window")
        self.assertEqual(report["competition"]["victory_requires"], "rank_1_and_roi_gt_5pct_after_fees")
        self.assertNotIn("details", report["scoreboard"][0])
        for private_key in ("cash", "cash_available", "exposure", "fees_paid", "penalties"):
            self.assertNotIn(private_key, report["scoreboard"][0])

    def test_status_report_runtime_complete_requires_replay_and_risk_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            recorder_dir = root / "recorder"
            reports_dir = recorder_dir / "reports"
            reports_dir.mkdir(parents=True)
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            (reports_dir / "aurum_stability_backup_status.json").write_text(
                json.dumps({"ok": True, "artifact_count": 3, "contains_manifest": True}),
                encoding="utf-8",
            )
            (reports_dir / "replay_summary.json").write_text(
                json.dumps({"ok": True, "mode": "runtime-proof", "risk_ledger_rows": 1}),
                encoding="utf-8",
            )
            recorder_health = {
                "ok": True,
                "age_seconds": 3,
                "errors": [],
                "last_capture": {
                    "market_count": 1,
                    "orderable_market_count": 1,
                    "sources": {"clob_book": {"ok_frames": 2}},
                    "manifest": {"ok": True, "verification_scope": "tail", "verified_rows": 10},
                    "book_coverage": {"requested_tokens": 2, "ok_tokens": 2},
                },
            }

            with mock.patch.object(aurum_status_report.data_quality_gate, "evaluate_data_quality_gate", return_value={"decision": "TRADE_ALLOWED", "ok": True, "trade_allowed": True, "hold_only": False, "stop_service": False, "reason_codes": []}), mock.patch.object(
                generate_dashboard.market_recorder, "recorder_health", return_value=recorder_health
            ):
                missing_ledger = aurum_status_report.build_report(data_dir, recorder_dir, max_stale_seconds=180)
                (data_dir / "risk_ledger.jsonl").write_text(json.dumps({"event": "fill"}) + "\n", encoding="utf-8")
                complete = aurum_status_report.build_report(data_dir, recorder_dir, max_stale_seconds=180)

        self.assertEqual(missing_ledger["risk_ledger"]["status"], "missing")
        self.assertEqual(missing_ledger["completion_state"], "code-complete-only")
        self.assertEqual(complete["replay"]["status"], "ok")
        self.assertEqual(complete["risk_ledger"]["status"], "present")
        self.assertEqual(complete["completion_state"], "runtime-complete")

    def test_status_report_explicit_recorder_dir_does_not_fallback_to_stale_data_dir_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            recorder_dir = root / "empty_explicit_recorder"
            stale_reports = data_dir / "reports"
            recorder_dir.mkdir(parents=True)
            stale_reports.mkdir(parents=True)
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            (data_dir / "risk_ledger.jsonl").write_text(json.dumps({"event": "fill"}) + "\n", encoding="utf-8")
            (stale_reports / "aurum_stability_backup_status.json").write_text(
                json.dumps({"ok": True, "artifact_count": 9, "contains_manifest": True}),
                encoding="utf-8",
            )
            (stale_reports / "replay_summary.json").write_text(
                json.dumps({"ok": True, "mode": "stale-data-dir", "risk_ledger_rows": 9}),
                encoding="utf-8",
            )
            recorder_health = {
                "ok": True,
                "age_seconds": 3,
                "errors": [],
                "last_capture": {
                    "market_count": 1,
                    "orderable_market_count": 1,
                    "sources": {"clob_book": {"ok_frames": 2}},
                    "manifest": {"ok": True, "verification_scope": "tail", "verified_rows": 10},
                    "book_coverage": {"requested_tokens": 2, "ok_tokens": 2},
                },
            }

            with mock.patch.object(aurum_status_report.data_quality_gate, "evaluate_data_quality_gate", return_value={"decision": "TRADE_ALLOWED", "ok": True, "trade_allowed": True, "hold_only": False, "stop_service": False, "reason_codes": []}), mock.patch.object(
                generate_dashboard.market_recorder, "recorder_health", return_value=recorder_health
            ):
                report = aurum_status_report.build_report(data_dir, recorder_dir, max_stale_seconds=180)

        self.assertEqual(report["backup"]["status"], "missing")
        self.assertEqual(report["replay"]["status"], "missing")
        self.assertEqual(report["completion_state"], "code-complete-only")

    def test_status_report_uses_explicit_recorder_dir_for_all_recorder_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            recorder_dir = root / "explicit_recorder_root"
            data_dir.mkdir(parents=True)
            recorder_dir.mkdir(parents=True)
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            (recorder_dir / "reports").mkdir(parents=True)
            (recorder_dir / "reports" / "aurum_stability_backup_status.json").write_text(
                json.dumps({"ok": True, "artifact_count": 7, "contains_manifest": True}),
                encoding="utf-8",
            )
            (recorder_dir / "reports" / "replay_summary.json").write_text(
                json.dumps({"ok": True, "mode": "explicit-root", "market_count": 3}),
                encoding="utf-8",
            )
            recorder_health = {
                "ok": True,
                "age_seconds": 3,
                "errors": [],
                "last_capture": {
                    "market_count": 1,
                    "orderable_market_count": 1,
                    "sources": {"clob_book": {"ok_frames": 2}},
                    "manifest": {"ok": True, "verification_scope": "tail", "verified_rows": 10},
                    "book_coverage": {"requested_tokens": 2, "ok_tokens": 2},
                },
            }

            with mock.patch.object(aurum_status_report.data_quality_gate, "evaluate_data_quality_gate", return_value={"decision": "TRADE_ALLOWED", "ok": True, "trade_allowed": True, "hold_only": False, "stop_service": False, "reason_codes": []}), mock.patch.object(
                generate_dashboard.market_recorder, "recorder_health", return_value=recorder_health
            ) as recorder_health_mock:
                report = aurum_status_report.build_report(data_dir, recorder_dir, max_stale_seconds=180)

        self.assertEqual(pathlib.Path(recorder_health_mock.call_args.args[0]), recorder_dir)
        self.assertEqual(report["backup"]["status"], "ok")
        self.assertEqual(report["backup"]["artifact_count"], 7)
        self.assertEqual(report["replay"]["mode"], "explicit-root")

    def test_dashboard_wires_tick_roi_series_into_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_dir = root / "paper_duel"
            out_dir = root / "public"
            agent_duel.init_state(data_dir, reset=True)
            bot_scripts.ensure_default_bot_scripts(data_dir)
            (data_dir / "ticks.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "tick_id": "t1",
                                "ts": "2026-06-14T03:30:00+00:00",
                                "mode": "paper_apply",
                                "scores": [
                                    {"agent_id": "superwing", "roi": 0.01},
                                    {"agent_id": "deepseek", "roi": -0.002},
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "tick_id": "t2",
                                "ts": "2026-06-14T03:31:00+00:00",
                                "mode": "paper_apply",
                                "scores": [
                                    {"agent_id": "superwing", "roi": 0.015},
                                    {"agent_id": "deepseek", "roi": 0.003},
                                ],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            path = generate_dashboard.render(argparse.Namespace(data_dir=str(data_dir), env_file="", output_dir=str(out_dir)))
            html = path.read_text(encoding="utf-8")

        self.assertIn('stroke="#a78bfa"', html)
        self.assertIn('stroke="#22d3ee"', html)

    def test_quant_lane_registry_supports_future_model_lanes_without_raw_peer_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            agent_duel.init_state(data_dir, reset=True)
            registry = quant_lanes.load_lane_registry(data_dir)

        for lane in ("superwing", "deepseek", "gpt", "claude", "manual"):
            self.assertIn(lane, registry["lanes"])
            self.assertFalse(registry["lanes"][lane]["raw_peer_ledger_allowed"])
            self.assertEqual(registry["lanes"][lane]["public_scope"], "coarse_aggregate_only")
        self.assertEqual(registry["lanes"]["gpt"]["status"], "dormant")
        self.assertEqual(quant_lanes.load_lane_control(data_dir, "gpt")["status"], "hold_only")

    def test_promotion_gate_passes_and_fails_against_baselines(self):
        baselines = quant_lanes.evaluate_baselines([{"value": 0.40}, {"value": 0.44}, {"value": 0.48}])
        passing = quant_lanes.evaluate_promotion_gate(
            {
                "schema_valid": True,
                "replay_ok": True,
                "holdout_ok": True,
                "after_fee_roi": quant_lanes.best_baseline_roi(baselines) + 0.02,
                "max_drawdown": 0.02,
                "trade_count": 4,
                "churn": 0.10,
                "exposure_concentration": 0.12,
            },
            baselines,
        )
        failing = quant_lanes.evaluate_promotion_gate(
            {
                "schema_valid": True,
                "replay_ok": True,
                "holdout_ok": True,
                "after_fee_roi": -0.01,
                "max_drawdown": 0.20,
                "trade_count": 0,
                "churn": 0.90,
                "exposure_concentration": 0.60,
            },
            baselines,
        )

        self.assertEqual(passing["status"], "pass")
        self.assertTrue(passing["executable"])
        self.assertEqual(failing["status"], "rejected")
        self.assertFalse(failing["executable"])
        self.assertIn("does_not_beat_best_baseline_after_fees", failing["reasons"])
        self.assertIn("drawdown_too_high", failing["reasons"])

    def test_black_swan_flow_protects_before_model_trigger_and_freezes_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            data_dir.mkdir(parents=True)
            reasons = quant_lanes.detect_extreme_events(
                market={"price_jump_abs": 0.22, "manifest_ok": False},
                account={"drawdown": 0.12, "repeated_stop_loss_or_rejects": 3},
                system={"restart_count": 3, "backup_ok": False},
            )
            record = quant_lanes.apply_black_swan_protection(data_dir, "deepseek", reasons, evidence={"remote_error": "redacted upstream"})
            control = quant_lanes.load_lane_control(data_dir, "deepseek")

        self.assertGreaterEqual(len(reasons), 5)
        self.assertEqual(record["flow"]["sequence"][0], "deterministic_protective_action")
        self.assertEqual(record["flow"]["sequence"][-1], "trigger_slow_model_review_for_resume_update_or_retire")
        self.assertFalse(record["flow"]["model_may_trade_in_hot_path"])
        self.assertEqual(control["status"], "frozen")

    def test_lane_freeze_blocks_trade_and_resume_requires_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            data_dir.mkdir(parents=True)
            control = quant_lanes.set_lane_control(data_dir, "superwing", "frozen", reason="unit")
            allowed, reasons = quant_lanes.lane_trade_allowed(data_dir, "superwing", script_tradable=True, gate_ok=True)
            blocked_resume, blocked_reasons = quant_lanes.can_resume_lane(control, {"status": "rejected", "executable": False})
            ok_resume, ok_reasons = quant_lanes.can_resume_lane(control, {"status": "pass", "executable": True})

        self.assertFalse(allowed)
        self.assertIn("lane_control_frozen", reasons)
        self.assertFalse(blocked_resume)
        self.assertIn("resume_gate_not_passed", blocked_reasons)
        self.assertTrue(ok_resume)
        self.assertEqual(ok_reasons, [])
    def test_btc_selector_prefers_variable_non_extreme_series_over_high_volume_flat_tail_market(self):
        records = []
        for idx, variable_price in enumerate([0.42, 0.47, 0.52], 1):
            records.append(
                {
                    "ts": f"2026-06-14T00:0{idx}:00+00:00",
                    "markets": [
                        {
                            "market_id": "btc-gta-million",
                            "question": "Will bitcoin hit $1m before GTA VI?",
                            "volume": 999999,
                            "liquidity": 999999,
                            "outcomes": [{"name": "Yes", "price": 0.01}, {"name": "No", "price": 0.99}],
                        },
                        {
                            "market_id": "btc-weekly-range",
                            "question": "Will Bitcoin close above this week's range?",
                            "volume": 1000,
                            "liquidity": 1000,
                            "outcomes": [{"name": "Yes", "price": variable_price}, {"name": "No", "price": 1 - variable_price}],
                        },
                    ],
                }
            )

        points, latest, source, diagnostic = generate_dashboard.bitcoin_series_from_snapshots(records)
        summary = generate_dashboard.btc_chart_summary(points)

        self.assertEqual(latest["market_id"], "btc-weekly-range")
        self.assertEqual(source, "btc_market_yes_price_variable_selector")
        self.assertEqual(summary["status"], "variable")
        self.assertIn("highest_volume_long_horizon", diagnostic["root_cause"])

    def test_operator_proposal_decision_requires_passed_gate_before_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            data_dir.mkdir(parents=True)
            review_id = "review001"
            proposal_path = strategy_rules.proposal_dir(data_dir) / f"{review_id}_superwing_bot_script.json"
            proposal_path.parent.mkdir(parents=True)
            proposal_path.write_text(json.dumps({"agent_id": "superwing"}), encoding="utf-8")
            review_path = data_dir / "strategy_reviews" / f"{review_id}.json"
            review_path.parent.mkdir(parents=True)
            review_path.write_text(
                json.dumps(
                    {
                        "promotion_gates": {
                            "superwing": {"status": "rejected", "executable": False, "reasons": ["does_not_beat_best_baseline_after_fees"]}
                        }
                    }
                ),
                encoding="utf-8",
            )

            proposals = quant_lanes.list_strategy_proposals(data_dir)
            self.assertEqual(len(proposals), 1)
            self.assertFalse(proposals[0]["can_approve"])
            with self.assertRaises(agent_duel.DuelError):
                quant_lanes.record_proposal_decision(data_dir, proposals[0]["proposal_id"], "approve")
            rejected = quant_lanes.record_proposal_decision(data_dir, proposals[0]["proposal_id"], "reject", reason="gate failed")

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["gate_status"], "rejected")

    def test_operator_proposal_decision_can_approve_after_gate_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            data_dir.mkdir(parents=True)
            review_id = "review002"
            proposal_path = strategy_rules.proposal_dir(data_dir) / f"{review_id}_deepseek_bot_script.json"
            proposal_path.parent.mkdir(parents=True)
            proposal_path.write_text(json.dumps({"agent_id": "deepseek"}), encoding="utf-8")
            review_path = data_dir / "strategy_reviews" / f"{review_id}.json"
            review_path.parent.mkdir(parents=True)
            review_path.write_text(
                json.dumps({"promotion_gates": {"deepseek": {"status": "pass", "executable": True, "reasons": []}}}),
                encoding="utf-8",
            )

            proposal = quant_lanes.list_strategy_proposals(data_dir)[0]
            approved = quant_lanes.record_proposal_decision(data_dir, proposal["proposal_id"], "approve", reason="gate passed")

        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["gate_executable"])

    def test_review_skip_guard_skips_no_change_and_high_quota_without_frozen_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            data_dir.mkdir(parents=True)
            (data_dir / "strategy_reviews").mkdir(parents=True)
            (data_dir / "strategy_reviews" / "last.json").write_text(json.dumps({"latest_tick_id": "tick-1"}), encoding="utf-8")
            base_ctx = {"recent_ticks": [{"tick_id": "tick-1"}]}
            no_change = strategy_review.review_skip_decision(data_dir, base_ctx)
            with mock.patch.dict(os.environ, {"AURUM_REVIEW_QUOTA_USED_PCT": "90", "AURUM_REVIEW_QUOTA_SKIP_PCT": "85"}, clear=False):
                quota_skip = strategy_review.review_skip_decision(data_dir, {"recent_ticks": [{"tick_id": "tick-2"}]})

        self.assertTrue(no_change["skip"])
        self.assertEqual(no_change["reason"], "no_new_tick_since_last_review")
        self.assertTrue(quota_skip["skip"])
        self.assertEqual(quota_skip["reason"], "quota_safe_skip")

    def test_black_swan_protection_closes_paper_position_before_freezing_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "paper_duel"
            state = agent_duel.init_state(data_dir, reset=True)
            market = {
                "market_id": "btc-1",
                "question": "Will Bitcoin close above range?",
                "outcomes": [{"name": "Yes", "price": 0.52}, {"name": "No", "price": 0.48}],
                "volume": 1000,
                "liquidity": 1000,
            }
            state["last_markets"] = [market]
            account = state["accounts"]["superwing"]
            account["cash"] = agent_duel.STARTING_EQUITY - 10.0
            account["positions"]["btc-1::Yes"] = {
                "market_id": "btc-1",
                "question": market["question"],
                "outcome": "Yes",
                "shares": 10.0,
                "cost_basis": 10.0,
                "avg_price": 1.0,
                "last_price": 0.52,
                "fees_paid": 0.0,
            }
            agent_duel.save_state(data_dir, state)

            record = quant_lanes.apply_black_swan_protection(data_dir, "superwing", ["market_price_jump"])
            final_state = agent_duel.load_state(data_dir)
            control = quant_lanes.load_lane_control(data_dir, "superwing")

        self.assertEqual(record["flow"]["sequence"][0], "deterministic_protective_action")
        self.assertGreaterEqual(record["flow"]["protective_result"]["fills"], 1)
        self.assertEqual(final_state["accounts"]["superwing"]["positions"], {})
        self.assertEqual(control["status"], "frozen")


if __name__ == "__main__":
    unittest.main()
