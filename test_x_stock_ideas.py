"""Offline behavioral tests: no X credentials, network requests or credits."""

import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import x_stock_ideas as app


def headers(**values):
    result = Message()
    for key, value in values.items():
        result[key.replace("_", "-")] = str(value)
    return result


class Response:
    def __init__(self, payload, response_headers=None):
        self.body = json.dumps(payload).encode()
        self.headers = response_headers or headers()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def error(status, body, response_headers=None):
    return urllib.error.HTTPError(app.ENDPOINT, status, "test", response_headers or headers(),
                                  io.BytesIO(json.dumps(body).encode()))


class QueueOpener:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep(self, value):
        self.sleeps.append(value)
        self.now += value


class FakeClient:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, params):
        self.calls.append(dict(params))
        if not self.pages:
            raise AssertionError("Unexpected API call; collection cap was exceeded.")
        return self.pages.pop(0)


def page(ids, next_token=None, text="$TEST valuation, revenue and earnings catalyst; downside risk."):
    return {"data": [{"id": str(post_id), "author_id": "10", "text": text,
                      "created_at": app.stamp(app.utcnow() - timedelta(hours=1))}
                     for post_id in ids],
            "includes": {"users": [{"id": "10", "username": "sample_author"}]},
            "meta": {"next_token": next_token} if next_token else {}}


class RateLimitTests(unittest.TestCase):
    def test_retry_wait_uses_later_of_reset_and_retry_after(self):
        self.assertEqual(app.retry_delay(headers(Retry_After=5, x_rate_limit_reset=1020), 0, 1000), 21)

    def test_http_date_retry_after_is_supported(self):
        value = "Thu, 01 Jan 1970 00:17:00 GMT"  # epoch 1020
        self.assertEqual(app.retry_delay(headers(Retry_After=value), 0, 1000), 21)

    def test_429_waits_and_retries(self):
        clock = Clock()
        opener = QueueOpener(error(429, {"title": "Too Many Requests"},
                                  headers(Retry_After=5, x_rate_limit_reset=1010)),
                             Response({"data": []}))
        client = app.XClient("secret", opener=opener, interval=0)
        with patch.object(app.time, "time", side_effect=lambda: clock.now), \
                patch.object(app.time, "sleep", side_effect=clock.sleep):
            client.get({"query": "test"})
        self.assertEqual(client.requests, 2)
        self.assertEqual(sum(clock.sleeps), 11)

    def test_success_with_zero_remaining_waits_before_next_request(self):
        clock = Clock()
        opener = QueueOpener(Response({}, headers(x_rate_limit_remaining=0, x_rate_limit_reset=1020)),
                             Response({}))
        client = app.XClient("secret", opener=opener, interval=0)
        with patch.object(app.time, "time", side_effect=lambda: clock.now), \
                patch.object(app.time, "sleep", side_effect=clock.sleep):
            client.get({"query": "test"})
            self.assertEqual(clock.sleeps, [])
            client.get({"query": "test"})
        self.assertEqual(sum(clock.sleeps), 21)

    def test_credit_errors_do_not_retry_or_wait(self):
        opener = QueueOpener(error(429, {"title": "UsageCapExceeded"}))
        client = app.XClient("secret", opener=opener)
        with patch.object(app.time, "sleep") as sleep, self.assertRaisesRegex(app.CollectorError, "billing"):
            client.get({"query": "test"})
        self.assertEqual(client.requests, 1)
        sleep.assert_not_called()

    def test_wait_budget_does_not_retry_early(self):
        opener = QueueOpener(error(429, {}, headers(Retry_After=120)))
        client = app.XClient("secret", opener=opener, max_wait=0)
        with patch.object(app.time, "sleep") as sleep, self.assertRaises(app.PauseCollection):
            client.get({"query": "test"})
        self.assertEqual(client.requests, 1)
        sleep.assert_not_called()

    def test_failed_attempts_count_toward_request_budget(self):
        opener = QueueOpener(error(503, {}), Response({}))
        client = app.XClient("secret", opener=opener, max_requests=1)
        with self.assertRaises(app.PauseCollection):
            client.get({"query": "test"})
        self.assertEqual(len(opener.calls), 1)

    def test_error_text_redacts_bearer_token(self):
        client = app.XClient("private-token", opener=QueueOpener(error(400, {"detail": "private-token"})))
        with self.assertRaises(app.CollectorError) as caught:
            client.get({"query": "test"})
        self.assertNotIn("private-token", str(caught.exception))


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.db = app.open_db(":memory:")

    def tearDown(self):
        self.db.close()

    def state(self):
        return json.loads(self.db.execute("SELECT state FROM cursors WHERE query='test'").fetchone()[0])

    def test_cap_saves_cursor_and_resume_does_not_skip_older_pages(self):
        first = FakeClient(page(range(101, 111), "next-page"))
        result = app.collect(self.db, first, "test", max_posts=10)
        self.assertFalse(result["complete"])
        before = self.state()
        self.assertIsNone(before["since_id"])
        self.assertEqual(before["high_id"], "110")

        second = FakeClient(page([90, 91]))
        app.collect(self.db, second, "test", max_posts=10)
        self.assertEqual(second.calls[0]["next_token"], "next-page")
        self.assertEqual(second.calls[0]["end_time"], first.calls[0]["end_time"])
        self.assertEqual(self.state()["since_id"], "110")

        third = FakeClient(page([120]))
        app.collect(self.db, third, "test", max_posts=10)
        self.assertEqual(third.calls[0]["since_id"], "110")
        self.assertNotIn("start_time", third.calls[0])
        self.assertNotIn("next_token", third.calls[0])

    def test_duplicate_posts_still_count_toward_return_cap(self):
        with self.db:
            app.save_posts(self.db, page(range(101, 111)))
        client = FakeClient(page(range(101, 111), "more"))
        result = app.collect(self.db, client, "test", max_posts=10)
        self.assertEqual(result["received"], 10)
        self.assertEqual(result["new"], 0)
        self.assertEqual(len(client.calls), 1)

    def test_wont_fetch_minimum_page_if_it_would_exceed_post_cap(self):
        client = FakeClient(page(range(10), "more"))
        result = app.collect(self.db, client, "test", max_posts=15)
        self.assertEqual(result["received"], 10)
        self.assertFalse(result["complete"])
        self.assertEqual(len(client.calls), 1)

    def test_page_and_checkpoint_roll_back_together_on_bad_id(self):
        payload = page([100, "bad-id"])
        with self.assertRaises(app.CollectorError):
            app.collect(self.db, FakeClient(payload), "test")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 0)

    def test_pagination_error_cannot_advance_high_water_mark(self):
        app.collect(self.db, FakeClient(page(range(101, 111), "next")), "test", max_posts=10)
        before = self.state()
        with self.assertRaises(app.CollectorError):
            app.collect(self.db, FakeClient({"errors": [{"title": "problem"}]}), "test")
        self.assertEqual(self.state(), before)

    def test_modern_and_legacy_long_form_text_are_preserved(self):
        for key in ("note_post", "note_tweet"):
            payload = page([100 if key == "note_post" else 101], text="truncated")
            payload["data"][0][key] = {"text": "$LONG full investment thesis and valuation",
                                      "entities": {"cashtags": [{"tag": "LONG"}]}}
            with self.db:
                app.save_posts(self.db, payload)
        rows = self.db.execute("SELECT text, tickers FROM posts").fetchall()
        self.assertTrue(all(row["text"].startswith("$LONG full") for row in rows))
        self.assertTrue(all(json.loads(row["tickers"]) == ["LONG"] for row in rows))

    def test_offline_export_escapes_csv_formulas_and_counts_copy_once(self):
        payload = page([100, 101], text="=HYPERLINK(\"https://example.invalid\") $TEST valuation and earnings catalyst")
        with self.db:
            app.save_posts(self.db, payload)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            count = app.export_reports(self.db, folder)
            with (folder / "stock_ideas.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(app.csv.DictReader(handle))
            self.assertEqual(count, 1)
            self.assertEqual(rows[0]["posts"], "1")
            self.assertTrue(rows[0]["example_text"].startswith("'="))
            self.assertEqual(len((folder / "posts.jsonl").read_text().splitlines()), 2)


class OfflineCliTests(unittest.TestCase):
    def test_demo_and_export_only_need_no_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("Unexpected network call")):
            self.assertEqual(app.main(["--demo", "--out", directory]), 0)
            demo = Path(directory) / "demo"
            self.assertTrue((demo / "stock_ideas.csv").is_file())
            self.assertEqual(app.main(["--export-only", "--out", str(demo)]), 0)

    def test_token_file_supports_quotes_and_does_not_evaluate_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.txt"
            path.write_text('# comment\nOTHER=ignore\nX_BEARER_TOKEN="a$(literal)token"\n')
            self.assertEqual(app.load_token(path), "a$(literal)token")


if __name__ == "__main__":
    unittest.main()
