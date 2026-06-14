import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import market_recorder


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, url, timeout=12.0):
        self.calls.append(url)
        if "gamma-api.polymarket.com" in url:
            return [
                {
                    "id": "m1",
                    "question": "Will Bitcoin close above 100k?",
                    "slug": "btc-100k",
                    "category": "Crypto",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.42", "0.58"]',
                    "clobTokenIds": '["tok_yes", "tok_no"]',
                    "volume": "12345",
                    "liquidity": "5000",
                }
            ]
        if "clob.polymarket.com/markets" in url:
            return {"markets": [{"condition_id": "cond1", "tokens": [{"token_id": "tok_yes"}]}]}
        if "data-api.polymarket.com/trades" in url:
            return [{"id": "trade1", "market": "m1", "price": 0.42, "size": 10}]
        if "clob.polymarket.com/book" in url:
            return {"market": "tok_yes", "bids": [["0.41", "100"]], "asks": [["0.43", "80"]]}
        raise AssertionError(f"unexpected URL {url}")


class MarketRecorderTests(unittest.TestCase):
    def test_capture_once_writes_raw_frames_and_hash_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            result = market_recorder.capture_once(
                data_dir,
                fetcher=FakeFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=1,
            )
            raw_dir = data_dir / "raw" / "polymarket" / "2026-06-14"
            manifest_path = raw_dir / "manifest.jsonl"
            health_path = data_dir / "reports" / "market_recorder_health.json"

            self.assertTrue((raw_dir / "gamma_markets.jsonl").exists())
            self.assertTrue((raw_dir / "clob_markets.jsonl").exists())
            self.assertTrue((raw_dir / "data_trades.jsonl").exists())
            self.assertTrue((raw_dir / "clob_books.jsonl").exists())
            self.assertTrue(manifest_path.exists())
            self.assertTrue(health_path.exists())
            latest_markets = json.loads((data_dir / "normalized" / "polymarket" / "latest_markets.json").read_text())
            self.assertEqual(latest_markets["source"], "polymarket_market_recorder_v0")
            self.assertEqual(latest_markets["markets"][0]["market_id"], "m1")
            self.assertEqual(latest_markets["markets"][0]["outcomes"][0]["token_id"], "tok_yes")

            manifest_lines = [json.loads(line) for line in manifest_path.read_text().splitlines()]
            self.assertEqual([row["source"] for row in manifest_lines], [
                "gamma_markets",
                "clob_markets",
                "data_trades",
                "clob_book",
            ])
            self.assertEqual(manifest_lines[0]["prev_manifest_sha256"], "")
            self.assertTrue(all(row["payload_sha256"] for row in manifest_lines))
            self.assertTrue(all(row["manifest_sha256"] for row in manifest_lines))
            self.assertEqual(manifest_lines[1]["prev_manifest_sha256"], manifest_lines[0]["manifest_sha256"])
            self.assertEqual(result["sources"]["clob_book"]["ok_frames"], 1)
            self.assertTrue(result["manifest"]["ok"])
            self.assertEqual(result["manifest"]["verification_scope"], "tail")
            self.assertEqual(result["manifest"]["max_rows"], 500)
            self.assertEqual(result["manifest"]["frame_tail_rows"], 2000)
            self.assertEqual(result["manifest"]["verified_rows"], 4)
            self.assertEqual(result["manifest"]["latest_sequence"], 4)
            self.assertEqual(result["source"], "polymarket_market_recorder_v0")
            self.assertEqual(result["book_coverage"]["coverage_ratio"], 1.0)

    def test_capture_once_writes_orderable_feed_and_full_book_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            result = market_recorder.capture_once(
                data_dir,
                fetcher=FakeFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=2,
            )
            feed = json.loads((data_dir / "features" / "polymarket_orderable_feed.json").read_text())
            latest = json.loads((data_dir / "normalized" / "polymarket" / "latest_markets.json").read_text())

        self.assertEqual(feed["requested_tokens"], 2)
        self.assertEqual(feed["ok_tokens"], 2)
        self.assertEqual(feed["orderable_tokens"], 2)
        self.assertEqual(feed["coverage_ratio"], 1.0)
        self.assertTrue(all(row["orderable"] for row in feed["markets"]))
        self.assertEqual(result["orderable_market_count"], 1)
        self.assertEqual(latest["book_coverage"]["ok_tokens"], 2)
        self.assertEqual(latest["orderable_market_count"], 1)

    def test_manifest_verifier_detects_tampered_raw_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            market_recorder.capture_once(
                data_dir,
                fetcher=FakeFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=1,
            )
            raw_path = data_dir / "raw" / "polymarket" / "2026-06-14" / "gamma_markets.jsonl"
            first_line = json.loads(raw_path.read_text().splitlines()[0])
            first_line["payload"] = {"tampered": True}
            raw_path.write_text(json.dumps(first_line, sort_keys=True) + "\n", encoding="utf-8")

            verified = market_recorder.verify_manifest(data_dir, ts="2026-06-14T03:30:00+00:00")

        self.assertFalse(verified["ok"])
        self.assertIn("manifest_frame_line_missing:1", verified["errors"])

    def test_manifest_verifier_rejects_external_frame_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "data"
            external = pathlib.Path(tmp) / "outside.jsonl"
            frame = {"ts": "2026-06-14T03:30:00+00:00", "source": "gamma_markets", "url": "file://outside", "ok": True, "status": "ok", "elapsed_ms": 0, "payload_sha256": market_recorder.sha256_text(market_recorder.canonical_json({"outside": True})), "payload": {"outside": True}}
            line = market_recorder.canonical_json(frame)
            external.write_text(line + "\n", encoding="utf-8")
            day_dir = data_dir / "raw" / "polymarket" / "2026-06-14"
            day_dir.mkdir(parents=True)
            manifest = {
                "sequence": 1,
                "ts": "2026-06-14T03:30:00+00:00",
                "source": "gamma_markets",
                "path": str(external),
                "line_sha256": market_recorder.sha256_text(line),
                "payload_sha256": frame["payload_sha256"],
                "prev_manifest_sha256": "",
            }
            manifest["manifest_sha256"] = market_recorder.sha256_text(market_recorder.canonical_json(manifest))
            (day_dir / "manifest.jsonl").write_text(market_recorder.canonical_json(manifest) + "\n", encoding="utf-8")

            verified = market_recorder.verify_manifest(data_dir, ts="2026-06-14T03:30:00+00:00")

        self.assertFalse(verified["ok"])
        self.assertIn("manifest_path_unsafe:1", verified["errors"])

    def test_manifest_verifier_rejects_unexpected_source_even_inside_raw_day_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp) / "data"
            day_dir = data_dir / "raw" / "polymarket" / "2026-06-14"
            day_dir.mkdir(parents=True)
            payload = {"evil": True}
            frame = {
                "ts": "2026-06-14T03:30:00+00:00",
                "source": "evil",
                "url": "https://example.invalid/evil",
                "ok": True,
                "status": "ok",
                "elapsed_ms": 0,
                "payload_sha256": market_recorder.sha256_text(market_recorder.canonical_json(payload)),
                "payload": payload,
            }
            line = market_recorder.canonical_json(frame)
            (day_dir / "evil.jsonl").write_text(line + "\n", encoding="utf-8")
            manifest = {
                "sequence": 1,
                "ts": "2026-06-14T03:30:00+00:00",
                "source": "evil",
                "path": "raw/polymarket/2026-06-14/evil.jsonl",
                "line_sha256": market_recorder.sha256_text(line),
                "payload_sha256": frame["payload_sha256"],
                "prev_manifest_sha256": "",
            }
            manifest["manifest_sha256"] = market_recorder.sha256_text(market_recorder.canonical_json(manifest))
            (day_dir / "manifest.jsonl").write_text(market_recorder.canonical_json(manifest) + "\n", encoding="utf-8")

            verified = market_recorder.verify_manifest(data_dir, ts="2026-06-14T03:30:00+00:00")

        self.assertFalse(verified["ok"])
        self.assertIn("manifest_source_unexpected:1", verified["errors"])

    def test_manifest_verifier_rejects_wrong_raw_path_for_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            market_recorder.capture_once(
                data_dir,
                fetcher=FakeFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=1,
            )
            manifest_path = data_dir / "raw" / "polymarket" / "2026-06-14" / "manifest.jsonl"
            rows = [json.loads(line) for line in manifest_path.read_text().splitlines()]
            rows[0]["path"] = "raw/polymarket/2026-06-14/clob_markets.jsonl"
            rows[0]["manifest_sha256"] = market_recorder.sha256_text(
                market_recorder.canonical_json({k: v for k, v in rows[0].items() if k != "manifest_sha256"})
            )
            manifest_path.write_text("\n".join(market_recorder.canonical_json(row) for row in rows) + "\n", encoding="utf-8")

            verified = market_recorder.verify_manifest(data_dir, ts="2026-06-14T03:30:00+00:00")

        self.assertFalse(verified["ok"])
        self.assertIn("manifest_path_unexpected:1", verified["errors"])

    def test_manifest_verifier_rejects_fractional_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            market_recorder.capture_once(
                data_dir,
                fetcher=FakeFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=1,
            )
            manifest_path = data_dir / "raw" / "polymarket" / "2026-06-14" / "manifest.jsonl"
            rows = [json.loads(line) for line in manifest_path.read_text().splitlines()]
            rows[0]["sequence"] = 1.5
            rows[0]["manifest_sha256"] = market_recorder.sha256_text(
                market_recorder.canonical_json({k: v for k, v in rows[0].items() if k != "manifest_sha256"})
            )
            manifest_path.write_text("\n".join(market_recorder.canonical_json(row) for row in rows) + "\n", encoding="utf-8")

            verified = market_recorder.verify_manifest(data_dir, ts="2026-06-14T03:30:00+00:00")

        self.assertFalse(verified["ok"])
        self.assertIn("manifest_sequence_error:1", verified["errors"])

    def test_manifest_tail_scope_reports_bounded_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            for minute in range(3):
                market_recorder.capture_once(
                    data_dir,
                    fetcher=FakeFetcher(),
                    now=lambda minute=minute: f"2026-06-14T03:3{minute}:00+00:00",
                    max_books=1,
                )

            verified = market_recorder.verify_manifest(
                data_dir,
                ts="2026-06-14T03:32:00+00:00",
                max_rows=4,
                frame_tail_rows=8,
            )

        self.assertTrue(verified["ok"])
        self.assertEqual(verified["verification_scope"], "tail")
        self.assertEqual(verified["max_rows"], 4)
        self.assertEqual(verified["frame_tail_rows"], 8)
        self.assertEqual(verified["verified_rows"], 4)
        self.assertEqual(verified["frames"], 4)
        self.assertEqual(verified["latest_sequence"], 12)

    def test_manifest_tail_scope_checks_previous_boundary_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            for minute in range(3):
                market_recorder.capture_once(
                    data_dir,
                    fetcher=FakeFetcher(),
                    now=lambda minute=minute: f"2026-06-14T03:3{minute}:00+00:00",
                    max_books=1,
                )
            manifest_path = data_dir / "raw" / "polymarket" / "2026-06-14" / "manifest.jsonl"
            rows = [json.loads(line) for line in manifest_path.read_text().splitlines()]
            rows[7]["payload_sha256"] = "tampered-boundary"
            manifest_path.write_text("\n".join(market_recorder.canonical_json(row) for row in rows) + "\n", encoding="utf-8")

            verified = market_recorder.verify_manifest(
                data_dir,
                ts="2026-06-14T03:32:00+00:00",
                max_rows=4,
                frame_tail_rows=8,
            )

        self.assertFalse(verified["ok"])
        self.assertIn("manifest_boundary_hash_error", verified["errors"])

    def test_manifest_full_scope_ignores_tail_raw_cache_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            for minute in range(2):
                market_recorder.capture_once(
                    data_dir,
                    fetcher=FakeFetcher(),
                    now=lambda minute=minute: f"2026-06-14T03:3{minute}:00+00:00",
                    max_books=1,
                )

            verified = market_recorder.verify_manifest(
                data_dir,
                ts="2026-06-14T03:31:00+00:00",
                max_rows=None,
                frame_tail_rows=1,
            )

        self.assertTrue(verified["ok"])
        self.assertEqual(verified["verification_scope"], "full")
        self.assertIsNone(verified["max_rows"])
        self.assertIsNone(verified["frame_tail_rows"])
        self.assertEqual(verified["verified_rows"], 8)
        self.assertEqual(verified["latest_sequence"], 8)

    def test_partial_book_fetch_keeps_requested_book_coverage_incomplete(self):
        class PartialBookFetcher(FakeFetcher):
            def __call__(self, url, timeout=12.0):
                if "clob.polymarket.com/book" in url and "tok_no" in url:
                    raise RuntimeError("book unavailable")
                return super().__call__(url, timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            result = market_recorder.capture_once(
                data_dir,
                fetcher=PartialBookFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=2,
            )
            feed = json.loads((data_dir / "features" / "polymarket_orderable_feed.json").read_text())
            latest = json.loads((data_dir / "normalized" / "polymarket" / "latest_markets.json").read_text())

        self.assertFalse(result["ok"])
        self.assertEqual(result["sources"]["clob_book"]["requested_tokens"], 2)
        self.assertEqual(result["sources"]["clob_book"]["ok_frames"], 1)
        self.assertEqual(result["book_coverage"]["requested_tokens"], 2)
        self.assertEqual(result["book_coverage"]["ok_tokens"], 1)
        self.assertEqual(feed["requested_tokens"], 2)
        self.assertEqual(feed["ok_tokens"], 1)
        self.assertEqual(feed["coverage_ratio"], 0.5)
        self.assertEqual(latest["book_coverage"]["requested_tokens"], 2)
        self.assertEqual(latest["book_coverage"]["ok_tokens"], 1)

    def test_complete_but_not_orderable_books_keep_capture_unhealthy(self):
        class EmptyBookFetcher(FakeFetcher):
            def __call__(self, url, timeout=12.0):
                if "clob.polymarket.com/book" in url:
                    return {"bids": [], "asks": []}
                return super().__call__(url, timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            result = market_recorder.capture_once(
                data_dir,
                fetcher=EmptyBookFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=2,
            )
            health = market_recorder.recorder_health(
                data_dir,
                now=lambda: "2026-06-14T03:30:20+00:00",
                max_stale_seconds=60,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["book_coverage"]["requested_tokens"], 2)
        self.assertEqual(result["book_coverage"]["ok_tokens"], 2)
        self.assertEqual(result["orderable_market_count"], 0)
        self.assertFalse(health["ok"])
        self.assertIn("orderable_market_count_invalid", health["errors"])

    def test_capture_once_records_orderbook_for_gamma_token_ids(self):
        fetcher = FakeFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            result = market_recorder.capture_once(
                pathlib.Path(tmp),
                fetcher=fetcher,
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=2,
            )

        book_calls = [url for url in fetcher.calls if "clob.polymarket.com/book" in url]
        self.assertEqual(len(book_calls), 2)
        self.assertTrue(any("token_id=tok_yes" in url for url in book_calls))
        self.assertTrue(any("token_id=tok_no" in url for url in book_calls))
        self.assertEqual(result["market_count"], 1)
        self.assertEqual(result["token_count"], 2)

    def test_health_summary_flags_recorder_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            health = market_recorder.recorder_health(data_dir, now=lambda: "2026-06-14T03:45:00+00:00")
            self.assertFalse(health["ok"])
            self.assertIn("missing_health_report", health["errors"])

            market_recorder.capture_once(
                data_dir,
                fetcher=FakeFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=1,
            )
            stale = market_recorder.recorder_health(data_dir, now=lambda: "2026-06-14T03:45:00+00:00", max_stale_seconds=60)
            self.assertFalse(stale["ok"])
            self.assertIn("recorder_stale", stale["errors"])

            fresh = market_recorder.recorder_health(data_dir, now=lambda: "2026-06-14T03:30:20+00:00", max_stale_seconds=60)
            self.assertTrue(fresh["ok"])
            self.assertEqual(fresh["last_capture"]["market_count"], 1)

    def test_health_summary_rejects_boolean_manifest_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            market_recorder.capture_once(
                data_dir,
                fetcher=FakeFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=1,
            )
            health_path = data_dir / "reports" / "market_recorder_health.json"
            payload = json.loads(health_path.read_text(encoding="utf-8"))
            payload["manifest"]["verified_rows"] = True
            payload["manifest"]["latest_sequence"] = True
            payload["manifest"]["max_rows"] = True
            health_path.write_text(json.dumps(payload), encoding="utf-8")

            health = market_recorder.recorder_health(
                data_dir,
                now=lambda: "2026-06-14T03:30:20+00:00",
                max_stale_seconds=60,
            )

        self.assertFalse(health["ok"])
        self.assertIn("manifest_verified_rows_invalid", health["errors"])

    def test_filters_to_bitcoin_markets_before_orderbooks(self):
        class MixedFetcher(FakeFetcher):
            def __call__(self, url, timeout=12.0):
                if "gamma" in url:
                    return [
                        {
                            "id": "other",
                            "question": "Will Rihanna release an album?",
                            "outcomes": '["Yes", "No"]',
                            "outcomePrices": '["0.51", "0.49"]',
                            "clobTokenIds": '["other_yes", "other_no"]',
                        },
                        {
                            "id": "btc",
                            "question": "Will Bitcoin hit 120k?",
                            "outcomes": '["Yes", "No"]',
                            "outcomePrices": '["0.44", "0.56"]',
                            "clobTokenIds": '["btc_yes", "btc_no"]',
                        },
                    ]
                return super().__call__(url, timeout=timeout)

        fetcher = MixedFetcher()
        with tempfile.TemporaryDirectory() as tmp:
            result = market_recorder.capture_once(
                pathlib.Path(tmp),
                fetcher=fetcher,
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=2,
            )
            latest = json.loads((pathlib.Path(tmp) / "normalized" / "polymarket" / "latest_markets.json").read_text())

        self.assertTrue(result["ok"])
        self.assertEqual([m["market_id"] for m in latest["markets"]], ["btc"])
        book_urls = [url for url in fetcher.calls if "/book?" in url]
        self.assertTrue(all("btc_" in url for url in book_urls), book_urls)

    def test_paper_engine_can_load_latest_recorder_markets(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            market_recorder.capture_once(
                data_dir,
                fetcher=FakeFetcher(),
                now=lambda: "2026-06-14T03:30:00+00:00",
                max_books=1,
            )
            loaded = market_recorder.load_latest_markets(
                data_dir,
                now=lambda: "2026-06-14T03:30:20+00:00",
                max_stale_seconds=60,
            )

        self.assertEqual(loaded["source"], "polymarket_market_recorder_v0")
        self.assertEqual(loaded["markets"][0]["market_id"], "m1")


if __name__ == "__main__":
    unittest.main()
