"""Offline Gemini integration tests; no real tokens, API calls or X sessions."""

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import discover
import gemini_analysis as ai
import x_stock_ideas as core
from company_directory import CompanyDirectory, parse_nasdaq
from test_x_stock_ideas import Clock, QueueOpener, Response, headers


def entry(post_id, text="$NVDA revenue improves. Liquid cooling is expanding."):
    return {"id": str(post_id), "text": text, "tags": ["$NVDA"]}


def answer(post_id="100", quote="Liquid cooling", existing_id=""):
    return {"id": post_id, "relevant": True,
            "companies": [{"mention": "$NVDA", "name": "NVIDIA Corporation", "ticker": "NVDA",
                           "confidence": "high", "evidence": "$NVDA"}],
            "themes": [{"existing_id": existing_id, "label": "Liquid cooling", "parent": "Data-centre infrastructure",
                        "confidence": "high", "evidence": quote}],
            "event": "A post reports expanding liquid cooling.", "stance": "bullish", "needs_context": False}


def completed(payload, reason="STOP"):
    return Response({"candidates": [{"finishReason": reason, "content": {"parts": [{"text": json.dumps(payload)}]}}],
                     "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 60, "totalTokenCount": 180}})


def http_error(code, payload, response_headers=None):
    return urllib.error.HTTPError("https://generativelanguage.googleapis.com/", code, "test",
                                  response_headers or headers(), io.BytesIO(json.dumps(payload).encode()))


class SnippetTests(unittest.TestCase):
    def test_three_sentences_with_negation_and_function_words_retained(self):
        text = "The demand rose and margins improved. It did not cut guidance. A contract was signed. Ignore this fourth sentence."
        snippet, truncated = ai.compact_snippet(text)
        self.assertEqual(snippet, "The demand rose and margins improved. It did not cut guidance. A contract was signed.")
        self.assertTrue(truncated)

    def test_decimals_codes_and_abbreviations_are_not_sentence_boundaries(self):
        text = "U.S. revenue rose 3.5% for BRK.B. Earnings improved. Guidance held. Fourth sentence."
        snippet, _ = ai.compact_snippet(text)
        self.assertEqual(snippet, "U.S. revenue rose 3.5% for BRK.B. Earnings improved. Guidance held.")

    def test_no_punctuation_has_a_character_cap(self):
        snippet, truncated = ai.compact_snippet("word " * 500, max_chars=100)
        self.assertLessEqual(len(snippet), 100)
        self.assertTrue(truncated)

    def test_urls_and_footers_removed_without_deleting_negation(self):
        snippet, _ = ai.compact_snippet("The thesis is not confirmed. https://example.com/a.b?utm_source=x\nRead more")
        self.assertEqual(snippet, "The thesis is not confirmed.")

    def test_link_tracking_normalized_but_article_identifier_retained(self):
        actual = ai.normalized_url("https://EXAMPLE.com/article?id=42&utm_source=x&fbclid=test#top")
        self.assertEqual(actual, "https://example.com/article?id=42")


class ValidationTests(unittest.TestCase):
    def test_fabricated_evidence_is_dropped_and_flagged(self):
        item = answer(quote="A made-up source excerpt")
        result = ai.validate_result({"items": [item]}, [entry("100")], [])
        self.assertEqual(result["100"]["themes"], [])
        self.assertTrue(result["100"]["needs_context"])

    def test_unknown_theme_id_is_rejected(self):
        with self.assertRaises(core.CollectorError):
            ai.validate_result({"items": [answer(existing_id="fake-id")]}, [entry("100")], [])

    def test_missing_result_cannot_be_assigned_to_the_wrong_post(self):
        with self.assertRaises(core.CollectorError):
            ai.validate_result({"items": [answer()]}, [entry("100"), entry("101")], [])

    def test_company_mention_must_be_supported_by_evidence(self):
        row = answer()
        row["companies"][0]["mention"] = "Tesla"
        result = ai.validate_result({"items": [row]}, [entry("100")], [])
        self.assertEqual(result["100"]["companies"], [])

    def test_irrelevant_post_cannot_add_themes_to_the_registry(self):
        row = answer()
        row["relevant"] = False
        result = ai.validate_result({"items": [row]}, [entry("100")], [])
        self.assertEqual(result["100"]["themes"], [])


class GeminiClientTests(unittest.TestCase):
    def setUp(self):
        self.db = core.open_db(":memory:")
        ai.init_tables(self.db)

    def tearDown(self):
        self.db.close()

    def test_rest_request_has_json_schema_and_key_in_header_only(self):
        opener = QueueOpener(completed({"items": [answer()]}))
        client = ai.GeminiClient("private-key", opener=opener, db=self.db)
        result = client.generate(ai.make_request([entry("100")], []))
        request = opener.calls[0]
        sent = json.loads(request.data)
        self.assertNotIn("private-key", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "private-key")
        self.assertEqual(sent["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("responseJsonSchema", sent["generationConfig"])
        self.assertNotIn("tools", sent)
        self.assertEqual(result["items"][0]["id"], "100")
        usage = self.db.execute("SELECT input_tokens,output_tokens,total_tokens FROM gemini_usage").fetchone()
        self.assertEqual(tuple(usage), (120, 60, 180))

    def test_daily_quota_stops_without_model_switch_or_retry(self):
        opener = QueueOpener(http_error(429, {"error": {"message": "GenerateRequestsPerDayPerProjectPerModel"}}))
        client = ai.GeminiClient("key", opener=opener, db=self.db)
        with patch.object(core.time, "sleep") as sleep, self.assertRaises(core.PauseCollection):
            client.generate(ai.make_request([entry("100")], []))
        self.assertEqual(len(opener.calls), 1)
        sleep.assert_not_called()

    def test_per_minute_quota_respects_retry_info(self):
        clock = Clock()
        error = http_error(429, {"error": {"details": [{"retryDelay": "20s"}]}})
        opener = QueueOpener(error, completed({"items": []}))
        client = ai.GeminiClient("key", opener=opener, db=self.db)
        with patch.object(core.time, "time", side_effect=lambda: clock.now), \
                patch.object(core.time, "sleep", side_effect=clock.sleep):
            client.generate(ai.make_request([], []))
        self.assertEqual(client.requests, 2)
        self.assertGreaterEqual(sum(clock.sleeps), 21)

    def test_request_cap_counts_failed_attempts(self):
        opener = QueueOpener(http_error(503, {}), completed({"items": []}))
        client = ai.GeminiClient("key", opener=opener, max_requests=1)
        with self.assertRaises(core.PauseCollection):
            client.generate(ai.make_request([], []))
        self.assertEqual(len(opener.calls), 1)

    def test_key_is_redacted_from_error_text(self):
        client = ai.GeminiClient("private-key", opener=QueueOpener(http_error(403, {"detail": "private-key"})))
        with self.assertRaises(core.CollectorError) as caught:
            client.generate(ai.make_request([], []))
        self.assertNotIn("private-key", str(caught.exception))

    def test_truncated_model_output_is_not_accepted(self):
        client = ai.GeminiClient("key", opener=QueueOpener(completed({}, reason="MAX_TOKENS")))
        with self.assertRaises(core.CollectorError):
            client.generate(ai.make_request([], []))

    def test_network_failure_is_not_automatically_replayed(self):
        opener = QueueOpener(urllib.error.URLError("test"), completed({}))
        client = ai.GeminiClient("key", opener=opener)
        with self.assertRaises(core.PauseCollection):
            client.generate(ai.make_request([], []))
        self.assertEqual(client.requests, 1)


class InterpretationCacheTests(unittest.TestCase):
    def setUp(self):
        self.db = core.open_db(":memory:")
        ai.init_tables(self.db)

    def tearDown(self):
        self.db.close()

    def add(self, ids, text=None, author="CNBC"):
        text = text or "$NVDA revenue improves. Liquid cooling is expanding. Guidance is unchanged. $AMD appears after the third sentence."
        with self.db:
            core.save_posts(self.db, {"data": [{"id": str(i), "author_id": str(i), "text": text,
                                               "created_at": core.stamp(core.utcnow() - timedelta(hours=1))}
                                              for i in ids],
                                      "includes": {"users": [{"id": str(i), "username": author} for i in ids]}})

    def test_exact_duplicates_are_interpreted_once_and_subsequent_runs_are_cached(self):
        self.add([100, 101])
        opener = QueueOpener(completed({"items": [answer()]}))
        client = ai.GeminiClient("key", opener=opener, db=self.db)
        result = ai.interpret_posts(self.db, client)
        self.assertEqual(result["new_distinct_inputs"], 1)
        self.assertEqual(client.requests, 1)
        no_calls = ai.GeminiClient("key", opener=QueueOpener(), db=self.db)
        ai.interpret_posts(self.db, no_calls)
        self.assertEqual(no_calls.requests, 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ai_post_links").fetchone()[0], 2)

    def test_cashtag_after_snippet_is_preserved_as_metadata(self):
        self.add([100])
        row = self.db.execute("SELECT * FROM posts").fetchone()
        item = ai.prepare_input(row)
        self.assertNotIn("$AMD", item["text"])
        self.assertIn("$AMD", item["tags"])
        self.assertTrue(item["truncated"])
        self.assertIn("$AMD", row["text"])

    def test_link_only_post_is_flagged_without_a_gemini_call_or_permanent_queue(self):
        self.add([100], "https://example.com/image-story")
        client = ai.GeminiClient("key", opener=QueueOpener(), db=self.db)
        ai.interpret_posts(self.db, client)
        interpreted, pending = ai.cached_interpretations(self.db, client.model)
        self.assertEqual(client.requests, 0)
        self.assertEqual(pending, 0)
        self.assertTrue(interpreted["100"]["needs_context"])
        self.assertEqual(interpreted["100"]["analysis_source"], "local_empty_input")

    def test_registry_id_is_reused_and_label_stays_stable(self):
        self.add([100])
        client = ai.GeminiClient("key", opener=QueueOpener(completed({"items": [answer()]})), db=self.db)
        ai.interpret_posts(self.db, client)
        theme_id = self.db.execute("SELECT id FROM ai_themes").fetchone()[0]
        self.add([102], "$NVDA reports new contracts. Liquid cooling expands further.")
        next_answer = answer("102", quote="Liquid cooling", existing_id=theme_id)
        next_answer["themes"][0]["label"] = "A reworded label"
        client = ai.GeminiClient("key", opener=QueueOpener(completed({"items": [next_answer]})), db=self.db)
        ai.interpret_posts(self.db, client)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ai_themes").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT label FROM ai_themes").fetchone()[0], "Liquid cooling")

    def test_unselected_authors_are_not_sent_to_gemini(self):
        self.add([100], author="unselected")
        client = ai.GeminiClient("key", opener=QueueOpener(), db=self.db)
        ai.interpret_posts(self.db, client, accounts={"cnbc"})
        self.assertEqual(client.requests, 0)

    def test_invalid_batch_does_not_poison_cache_or_create_themes(self):
        self.add([100])
        client = ai.GeminiClient("key", opener=QueueOpener(completed({"items": []})), db=self.db)
        with self.assertRaises(core.CollectorError):
            ai.interpret_posts(self.db, client)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ai_cache").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ai_themes").fetchone()[0], 0)

    def test_pending_interpretations_withhold_novelty_labels(self):
        with patch.object(core, "utcnow", return_value=core.utcnow()):
            discover.seed_demo(self.db)
        ai.interpret_posts(self.db, ai.DemoGemini(), max_posts=100)
        interpreted, _ = ai.cached_interpretations(self.db, "demo-gemini")
        one = next(iter(interpreted))
        del interpreted[one]
        report = discover.analyse_discovery(self.db, interpretations=interpreted)
        self.assertGreater(report["metrics"]["pending_interpretations_in_window"], 0)
        self.assertFalse(report["metrics"]["baseline_ready"])
        self.assertTrue(all(r["status"] == "insufficient_baseline" for r in report["stocks"]))


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = CompanyDirectory([
            {"ticker": "NVDA", "name": "NVIDIA Corporation - Common Stock", "aliases": "Nvidia"},
            {"ticker": "TSLA", "name": "Tesla Inc.", "aliases": "Tesla"},
            {"ticker": "AI", "name": "C3.ai Inc.", "aliases": ""},
            {"ticker": "GOOG", "name": "Alphabet Inc.", "aliases": "Google"},
            {"ticker": "GOOGL", "name": "Alphabet Inc.", "aliases": "Google"},
        ])

    def item(self, mention, name, ticker):
        return {"mention": mention, "name": name, "ticker": ticker, "confidence": "high", "evidence": mention}

    def test_company_name_without_a_cashtag_resolves_against_reference(self):
        result = self.directory.resolve(self.item("Nvidia", "NVIDIA Corporation", ""), "Nvidia reports earnings.")
        self.assertEqual(result["ticker"], "NVDA")
        self.assertEqual(result["mapping_status"], "reference_name")

    def test_bare_ticker_is_validated(self):
        result = self.directory.resolve(self.item("NVDA", "NVIDIA", "NVDA"), "NVDA reports earnings.")
        self.assertEqual(result["ticker"], "NVDA")

    def test_ambiguous_share_class_is_not_guessed(self):
        result = self.directory.resolve(self.item("Google", "Alphabet", "GOOG"), "Google reports earnings.")
        self.assertEqual(result["ticker"], "")
        self.assertEqual(result["mapping_status"], "ambiguous_share_class")

    def test_generic_ai_word_is_not_automatically_a_stock(self):
        result = self.directory.resolve(self.item("AI", "C3.ai", "AI"), "AI power demand keeps growing.")
        self.assertEqual(result["ticker"], "")

    def test_source_company_match_overrides_incorrect_model_ticker(self):
        result = self.directory.resolve(self.item("Tesla", "Tesla", "NVDA"), "Tesla reports earnings.")
        self.assertEqual(result["ticker"], "TSLA")

    def test_nasdaq_parser_excludes_test_issues_and_etfs(self):
        text = "Symbol|Security Name|Test Issue|ETF\nNVDA|NVIDIA Corporation|N|N\nTEST|Test issue|Y|N\nFUND|Fund|N|Y\nFile Creation Time: 11102026|||\n"
        self.assertEqual([r["ticker"] for r in parse_nasdaq(text)], ["NVDA"])


class SettingsTests(unittest.TestCase):
    def test_key_and_model_are_read_from_credentials_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.txt"
            path.write_text('X_USERNAME=user\nGEMINI_API_KEY="test-key"\nGEMINI_MODEL=gemini-3.1-flash-lite\n')
            self.assertEqual(ai.load_settings(path), ("test-key", "gemini-3.1-flash-lite"))

    def test_accounts_file_becomes_author_filters_without_a_stock_watchlist(self):
        path = Path(__file__).parent / "accounts.txt"
        handles = discover.load_accounts(path)
        self.assertEqual(len(handles), 10)
        self.assertIn("CNBC", handles)
        self.assertNotIn("NVDA", handles)


class PipelineTests(unittest.TestCase):
    def test_analyze_only_resolves_named_companies_and_export_only_uses_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "credentials.txt"
            credentials.write_text("GEMINI_API_KEY=test-key\n")
            reference = root / "symbols.csv"
            reference.write_text("ticker,name,aliases\nNVDA,NVIDIA Corporation,Nvidia\n")
            db = core.open_db(root / "posts.sqlite3")
            with db:
                core.save_posts(db, {"data": [
                    {"id": "100", "author_id": "1", "created_at": core.stamp(core.utcnow() - timedelta(hours=1)),
                     "text": "Nvidia introduces liquid cooling. The new design addresses GPU heat."},
                    {"id": "101", "author_id": "2", "created_at": core.stamp(core.utcnow() - timedelta(hours=2)),
                     "text": "Nvidia expands liquid cooling. More systems are entering production."}],
                    "includes": {"users": [{"id": "1", "username": "CNBC"}, {"id": "2", "username": "Benzinga"}]}})
            db.close()
            first, second = answer("100", "liquid cooling"), answer("101", "liquid cooling")
            for value in (first, second):
                value["companies"][0].update({"mention": "Nvidia", "evidence": "Nvidia"})
            opener = QueueOpener(completed({"items": [first, second]}))
            actual_client = ai.GeminiClient
            def factory(*args, **kwargs):
                return actual_client(*args, opener=opener, **kwargs)
            arguments = ["--analysis", "gemini", "--analyze-only", "--out", str(root), "--credentials", str(credentials),
                         "--company-map", str(reference), "--offline-symbols"]
            with patch.object(ai, "GeminiClient", side_effect=factory):
                self.assertEqual(discover.main(arguments), 0)
            self.assertEqual(len(opener.calls), 1)
            report = json.loads((root / "discovery.json").read_text())
            self.assertEqual(report["stocks"][0]["candidate"], "NVDA")
            self.assertEqual(report["stocks"][0]["distinct_accounts"], 2)
            self.assertEqual(report["metrics"]["analysis_mode"], "gemini")
            self.assertFalse(report["metrics"]["baseline_ready"])
            # The next invocation reuses every interpretation; no second response exists.
            with patch.object(ai, "GeminiClient", side_effect=factory):
                self.assertEqual(discover.main(arguments), 0)
            self.assertEqual(len(opener.calls), 1)
            credentials.unlink()
            with patch.object(core.urllib.request, "urlopen", side_effect=AssertionError("network")):
                self.assertEqual(discover.main(["--analysis", "gemini", "--export-only", "--out", str(root), "--company-map", str(reference)]), 0)

    def test_assembled_request_character_budget_splits_batches(self):
        db = core.open_db(":memory:")
        try:
            with db:
                core.save_posts(db, {"data": [{"id": str(100 + i), "text": "word " * 70 + f"reference {i}",
                                              "created_at": core.stamp(core.utcnow())} for i in range(4)]})
            prepared = ai.prepare_input(db.execute("SELECT * FROM posts ORDER BY id LIMIT 1").fetchone())
            bound = len(json.dumps(ai.make_request([prepared], []), ensure_ascii=False)) + 50
            class IrrelevantClient:
                model = "test-model"
                sizes = []
                def generate(self, request):
                    self.sizes.append(len(json.dumps(request, ensure_ascii=False)))
                    posts = json.loads(request["contents"][0]["parts"][0]["text"])["posts"]
                    return {"items": [{"id": p["id"], "relevant": False, "companies": [], "themes": [],
                                       "event": "", "stance": "unclear", "needs_context": False} for p in posts]}
            client = IrrelevantClient()
            ai.interpret_posts(db, client, max_prompt_chars=bound)
            self.assertEqual(len(client.sizes), 4)
            self.assertTrue(all(size <= bound for size in client.sizes))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
