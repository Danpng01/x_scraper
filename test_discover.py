"""Discovery and free-source behavior, using fictional posts and mock sessions."""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discover
import x_stock_ideas as core
from test_x_stock_ideas import FakeClient, page
from twikit_source import TwikitSource


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = core.open_db(":memory:")
        self.now = core.utcnow()

    def tearDown(self):
        self.db.close()

    def add(self, symbol, author, hours, index, text=None):
        payload = {"data": [{"id": str(700000 + index), "author_id": str(author),
                             "created_at": core.stamp(self.now - timedelta(hours=hours)),
                             "text": text or f"${symbol}: molecular spintronics investment thesis, "
                             f"revenue and valuation. Reference {index}."}]}
        with self.db:
            core.save_posts(self.db, payload)

    def analyse(self, **kwargs):
        return discover.analyse_discovery(self.db, now=self.now, **kwargs)

    def test_demo_distinguishes_new_rising_and_established(self):
        with patch.object(core, "utcnow", return_value=self.now):
            discover.seed_demo(self.db)
        report = self.analyse()
        statuses = {r["candidate"]: r["status"] for r in report["stocks"]}
        self.assertTrue(report["metrics"]["baseline_ready"])
        self.assertEqual(statuses["NEW"], "new_in_sample")
        self.assertEqual(statuses["RISE"], "rising_in_sample")
        self.assertNotIn("BASE", statuses)
        self.assertEqual({r["candidate"] for r in report["themes"]}, {"orbital cooling", "photon weaving"})

    def test_cold_start_does_not_claim_novelty_or_momentum(self):
        self.add("NEW", 1, 1, 1)
        self.add("NEW", 2, 2, 2)
        report = self.analyse()
        self.assertFalse(report["metrics"]["baseline_ready"])
        self.assertEqual(report["stocks"][0]["status"], "insufficient_baseline")
        self.assertIsNone(report["stocks"][0]["attention_lift"])

    def test_a_single_account_cannot_create_a_ranked_theme(self):
        for index in range(10):
            self.add("NEW", 1, index + 1, index)
        report = self.analyse()
        self.assertEqual(report["stocks"], [])
        self.assertEqual(report["themes"], [])

    def test_exact_copies_do_not_count_as_distinct_support(self):
        text = "$NEW molecular spintronics thesis and valuation."
        self.add("NEW", 1, 1, 1, text)
        self.add("NEW", 2, 2, 2, text)
        self.assertEqual(self.analyse()["stocks"], [])

    def test_same_text_in_previous_window_is_not_erased(self):
        text = "$NEW molecular spintronics thesis and valuation."
        self.add("NEW", 1, 1, 1, text)
        self.add("NEW", 2, 2, 2)
        self.add("NEW", 3, 72, 3, text)
        report = self.analyse(min_baseline_posts=1, include_established=True)
        row = report["stocks"][0]
        self.assertEqual(row["baseline_posts"], 1)
        self.assertNotEqual(row["status"], "new_in_sample")

    def test_old_historical_mention_means_returning_not_new(self):
        with patch.object(core, "utcnow", return_value=self.now):
            discover.seed_demo(self.db)
        self.add("NEW", 99, 24 * 20, 777)
        row = next(r for r in self.analyse()["stocks"] if r["candidate"] == "NEW")
        self.assertEqual(row["status"], "returning_in_sample")

    def test_future_and_promotional_posts_are_excluded(self):
        self.add("NEW", 1, -24, 1)
        self.add("NEW", 2, 1, 2, "$NEW thesis valuation revenue, guaranteed returns! Join my telegram.")
        self.assertEqual(self.analyse()["metrics"]["recent_qualifying_posts"], 0)

    def test_phrases_are_discovered_without_a_theme_dictionary(self):
        phrases = discover.theme_phrases("$NEW: molecular spintronics. An investment thesis. https://example.com/hello-world")
        self.assertIn("molecular spintronics", phrases)
        self.assertNotIn("investment thesis", phrases)
        self.assertFalse(any("example" in phrase for phrase in phrases))

    def test_lowercase_cashtags_are_normalized(self):
        tickers, _, _ = core.analyse("$nvda $Amd $brk.b $100 $usdt", {})
        self.assertEqual(tickers, ["AMD", "BRK.B", "NVDA", "USDT"])

    def test_invalid_universe_does_not_silently_export_unfiltered_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad.txt"
            bad.write_text("123\n")
            self.assertEqual(discover.main(["--export-only", "--out", str(root), "--universe", str(bad)]), 1)
            self.assertFalse((root / "emerging_stocks.csv").exists())

    def test_equity_universe_can_filter_ticker_candidates(self):
        with patch.object(core, "utcnow", return_value=self.now):
            discover.seed_demo(self.db)
        report = self.analyse(universe={"RISE"})
        self.assertEqual([r["candidate"] for r in report["stocks"]], ["RISE"])

    def test_sampling_allocations_stay_within_global_budget(self):
        jobs = discover.collection_jobs(discover.QUERIES, "twikit", True, 24, 6, 400, self.now)
        self.assertLessEqual(sum(job["max_posts"] for job in jobs), 400)
        self.assertTrue(all(job["max_posts"] % 20 == 0 for job in jobs))
        old_slices = {job["start_time"] for job in jobs if job["fixed_window"]}
        self.assertGreaterEqual(len(old_slices), 6)
        self.assertFalse(any("$" in job["query"] for job in jobs))

    def test_discovery_refresh_prioritizes_latest_but_retains_completed_watermark(self):
        state = {"since_id": "80", "active": True, "next_token": "old-page", "high_id": "100"}
        with self.db:
            self.db.execute("INSERT INTO cursors VALUES (?,?)", ("discovery:key", json.dumps(state)))
        discover.refresh_live_snapshot(self.db, "discovery:key")
        actual = json.loads(self.db.execute("SELECT state FROM cursors").fetchone()[0])
        self.assertFalse(actual["active"])
        self.assertEqual(actual["since_id"], "80")
        self.assertNotIn("next_token", actual)

    def test_finished_fixed_backfill_is_not_repeated(self):
        start, end = core.stamp(self.now - timedelta(days=3)), core.stamp(self.now - timedelta(days=2))
        core.collect(self.db, FakeClient(page([100])), "q", start_time=start, end_time=end,
                     cursor_key="fixed", fixed_window=True)
        result = core.collect(self.db, FakeClient(), "q", start_time=start, end_time=end,
                              cursor_key="fixed", fixed_window=True)
        self.assertEqual(result["received"], 0)

    def test_filtered_results_still_consume_collection_budget(self):
        client = FakeClient({"data": [], "meta": {"next_token": "more"}, "_returned_count": 20})
        client.minimum_page = 20
        result = core.collect(self.db, client, "q", max_posts=20)
        self.assertEqual(result["received"], 20)
        self.assertEqual(len(client.calls), 1)

    def test_demo_is_offline_and_exports_both_discovery_outputs(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(core.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.assertEqual(discover.main(["--demo", "--out", directory]), 0)
            folder = Path(directory) / "demo"
            self.assertTrue((folder / "emerging_stocks.csv").is_file())
            self.assertTrue((folder / "emerging_themes.csv").is_file())
            self.assertIn("Fictional demonstration", (folder / "discovery_report.md").read_text())


class FreeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cookies = Path(self.tmp.name) / "cookies.json"
        self.cookies.write_text("{}")
        self.now = core.utcnow()
        self.calls = []
        outer = self

        class Session:
            def load_cookies(self, path):
                outer.calls.append(("cookies", path))

            async def search_tweet(self, **kwargs):
                outer.calls.append(("search", kwargs))
                return outer.result

        self.session = Session()
        self.source = TwikitSource(self.cookies, Path(self.tmp.name) / "missing.txt", client=self.session)
        self.source.interval = 0

    def tearDown(self):
        self.source.close()
        self.tmp.cleanup()

    def tweet(self, post_id, hours):
        return SimpleNamespace(id=str(post_id), created_at_datetime=self.now - timedelta(hours=hours),
                               user=SimpleNamespace(id="1", screen_name="test_author"),
                               text="truncated", full_text="$NEW full valuation thesis", conversation_id=str(post_id))

    def result_of(self, posts, token="more"):
        class Result(list):
            next_cursor = token
        return Result(posts)

    def params(self):
        return {"query": "thesis -is:retweet -is:reply", "max_results": 20,
                "start_time": core.stamp(self.now - timedelta(days=1)),
                "end_time": core.stamp(self.now - timedelta(seconds=30))}

    def test_saved_session_and_latest_search_no_paid_api(self):
        self.result = self.result_of([self.tweet(100, 1)])
        payload = self.source.get(self.params())
        options = self.calls[-1][1]
        self.assertEqual(options["product"], "Latest")
        self.assertIn("-filter:retweets", options["query"])
        self.assertIn("since:", options["query"])
        self.assertEqual(payload["data"][0]["text"], "$NEW full valuation thesis")
        self.assertEqual(payload["meta"]["next_token"], "more")

    def test_day_granular_results_are_filtered_but_counted(self):
        self.result = self.result_of([self.tweet(100, 1), self.tweet(90, 30), self.tweet(110, -1)])
        payload = self.source.get(self.params())
        self.assertEqual(payload["_returned_count"], 3)
        self.assertEqual([p["id"] for p in payload["data"]], ["100"])

    def test_cursor_is_passed_to_next_search(self):
        self.result = self.result_of([self.tweet(100, 1)])
        params = self.params()
        params["next_token"] = "saved-cursor"
        self.source.get(params)
        self.assertEqual(self.calls[-1][1]["cursor"], "saved-cursor")

    def test_login_or_challenge_error_is_not_retried_or_printed(self):
        class Forbidden(Exception):
            pass

        async def denied(**kwargs):
            raise Forbidden("secret-password-or-cookie")

        self.source.client.search_tweet = denied
        with self.assertRaises(core.CollectorError) as caught:
            self.source.get(self.params())
        self.assertEqual(self.source.requests, 1)
        self.assertNotIn("secret-password-or-cookie", str(caught.exception))

    def test_rate_limiter_stops_when_wait_budget_is_zero(self):
        class TooManyRequests(Exception):
            headers = {"retry-after": "120"}

        async def limited(**kwargs):
            raise TooManyRequests()

        self.source.client.search_tweet = limited
        self.source.max_wait = 0
        with self.assertRaises(core.PauseCollection):
            self.source.get(self.params())
        self.assertEqual(self.source.requests, 1)

    def test_generic_exception_exposes_phase_and_redacts_known_secrets(self):
        self.source.secrets.update({"private-password", "session-secret"})
        async def login(**kwargs):
            raise Exception("LoginFlow unavailable; password=private-password auth_token=session-secret")
        with self.assertRaises(core.CollectorError) as caught:
            self.source.call(login, retry=False)
        detail = str(caught.exception)
        self.assertIn("X login failed", detail)
        self.assertIn("LoginFlow unavailable", detail)
        self.assertNotIn("private-password", detail)
        self.assertNotIn("session-secret", detail)

    def test_generic_search_failure_is_not_retried_as_login(self):
        async def search_tweet(**kwargs):
            raise Exception("Search operation unavailable")
        self.source.client.search_tweet = search_tweet
        with self.assertRaisesRegex(core.CollectorError, "X search failed"):
            self.source.get(self.params())
        self.assertEqual(self.source.requests, 1)


if __name__ == "__main__":
    unittest.main()
