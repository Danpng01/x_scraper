#!/usr/bin/env python3
"""Collect X stock research with bounded reads, rate-limit handling and resume.

Python 3.10+, standard library only. Run --demo without credentials or API calls.
See README.md for setup, costs, limits and the interpretation of the shortlist.
"""

import argparse
import csv
import html
import json
import math
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


ENDPOINT = "https://api.x.com/2/tweets/search/recent"
DEFAULT_QUERY = (
    '("investment thesis" OR "bull case" OR "bear case" OR undervalued OR catalyst) '
    'lang:en -is:retweet -is:reply'
)
TICKER = re.compile(r"[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z]{1,2})?\Z")
CASHTAG = re.compile(r"(?<![\w$])\$([A-Z][A-Z0-9]{0,5}(?:[.-][A-Z]{1,2})?)(?![\w.-])", re.I)
SIGNALS = {
    "thesis": (2, r"\b(thesis|bull case|bear case|mispriced|variant view)\b"),
    "valuation": (3, r"\b(valuation|undervalued|overvalued|free cash flow|ev/ebitda|p/e|fcf yield)\b"),
    "catalyst": (2, r"\b(catalyst|earnings|approval|spin.off|buyback|contract|guidance)\b"),
    "fundamentals": (2, r"\b(revenue|margin|cash flow|backlog|balance sheet|debt)\b"),
    "counterargument": (2, r"\b(risk|downside|dilution|bear case|competition|what could go wrong)\b"),
}
PROMO = re.compile(r"\b(telegram|whatsapp|guaranteed returns|100x|dm me|join my|pump)\b", re.I)


class CollectorError(Exception):
    pass


class PauseCollection(CollectorError):
    """A bounded run stopped; the saved cursor can be resumed."""


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def log(message):
    print(message, file=sys.stderr, flush=True)


def load_token(path):
    """Read exactly X_BEARER_TOKEN from a key=value file, without evaluating it."""
    from config import load_config
    token = load_config(path).get("X_BEARER_TOKEN", "")
    if token and not token.startswith("PASTE_") and not re.search(r"\s", token):
        return token
    raise CollectorError(f"X_BEARER_TOKEN was not found in {path}.")


def retry_delay(headers, attempt, now=None):
    """Respect BOTH Retry-After and X's reset time; never shorten a server wait."""
    now = time.time() if now is None else now
    candidates = []
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            candidates.append(float(retry_after))
        except (ValueError, TypeError):
            try:
                candidates.append(parsedate_to_datetime(retry_after).timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
    try:
        candidates.append(float(headers["x-rate-limit-reset"]) - now)
    except (KeyError, ValueError, TypeError):
        pass
    candidates = [v for v in candidates if math.isfinite(v) and v >= 0]
    return max(candidates) + 1 if candidates else min(60 * (2 ** attempt), 900)


class XClient:
    def __init__(self, token, max_requests=10, max_wait=900, retries=3,
                 interval=2.1, opener=None):
        self.token = token
        self.max_requests = max_requests
        self.max_wait = max_wait
        self.retries = retries
        self.interval = interval
        self.opener = opener or urllib.request.urlopen
        self.requests = 0
        self.waited = 0.0
        self.not_before = 0.0

    def wait(self, seconds, reason):
        seconds = max(0.0, seconds)
        if seconds <= 0:
            return
        if self.waited + seconds > self.max_wait:
            raise PauseCollection(
                f"{reason}: would wait {seconds:.0f}s, exceeding --max-wait. "
                "Progress is saved; rerun later or increase --max-wait."
            )
        if seconds >= 5:
            log(f"{reason}: waiting {seconds:.0f}s (Ctrl+C stops safely).")
        self.waited += seconds
        # Short chunks keep Ctrl+C responsive on all supported platforms.
        remaining = seconds
        while remaining > 0:
            chunk = min(remaining, 30)
            time.sleep(chunk)
            remaining -= chunk

    def get(self, params):
        url = ENDPOINT + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json", "User-Agent": "x-stock-ideas/1.0",
        })
        for attempt in range(self.retries + 1):
            if self.requests >= self.max_requests:
                raise PauseCollection("Reached --max-requests, including retries. Rerun to resume.")
            self.wait(self.not_before - time.time(), "Request pacing / rate-limit reset")
            self.requests += 1
            try:
                with self.opener(request, timeout=30) as response:
                    body = response.read()
                    headers = response.headers
                self.not_before = time.time() + self.interval
                if headers.get("x-rate-limit-remaining") == "0":
                    self.not_before = max(self.not_before, time.time() + retry_delay(headers, 0))
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    # Do not automatically repeat a potentially billed successful read.
                    raise CollectorError("X returned invalid JSON; stopping to avoid repeated reads.") from exc
                if not isinstance(payload, dict):
                    raise CollectorError("Unexpected X response format; stopping without retrying.")
                return payload
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace").replace(self.token, "[REDACTED]")
                text = body.lower()
                credit_markers = (
                    "usagecapexceeded", "usage-capped", "usage cap", "creditsdepleted",
                    "insufficient credits", "out of credits", "credits depleted",
                    "spending limit", "billing limit", "monthly cap",
                )
                if exc.code == 402 or any(marker in text for marker in credit_markers):
                    raise CollectorError(
                        "X billing/usage allowance is exhausted. Waiting for a rate reset cannot "
                        "fix this; check credits and spending limits in console.x.com. "
                        f"HTTP {exc.code}: {body[:400]}"
                    ) from exc
                if exc.code in (401, 403):
                    raise CollectorError(
                        f"X rejected authentication/access (HTTP {exc.code}). Check the Bearer "
                        f"Token and app access in console.x.com. {body[:400]}"
                    ) from exc
                if exc.code not in (429, 500, 502, 503, 504):
                    raise CollectorError(f"X HTTP {exc.code}: {body[:500]}") from exc
                if attempt == self.retries:
                    raise PauseCollection(f"X HTTP {exc.code}: retry limit reached. Rerun later.") from exc
                delay = (retry_delay(exc.headers, attempt) if exc.code == 429
                         or exc.headers.get("Retry-After") else min(2 ** (attempt + 1), 60))
                self.not_before = time.time() + delay
                log(f"X HTTP {exc.code}; respecting a {delay:.0f}s retry delay.")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self.retries:
                    raise PauseCollection("Network retries exhausted. Progress is saved; rerun later.") from exc
                self.not_before = time.time() + min(2 ** (attempt + 1), 60)


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY, created_at TEXT, author_id TEXT, username TEXT,
            text TEXT, tickers TEXT, evidence_score INTEGER, signals TEXT,
            url TEXT, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS cursors (query TEXT PRIMARY KEY, state TEXT NOT NULL);
    """)
    return db


def analyse(text, entities):
    tickers = {tag.upper() for tag in CASHTAG.findall(text)}
    for item in entities.get("cashtags", []):
        tag = str(item.get("tag", "")).upper()
        if TICKER.fullmatch(tag):
            tickers.add(tag)
    signals, score = [], 0
    for name, (weight, pattern) in SIGNALS.items():
        if re.search(pattern, text, flags=re.I):
            signals.append(name)
            score += weight
    if len(text) >= 180:
        score += 1
    if PROMO.search(text):
        signals.append("promotional_language")
        score -= 5
    return sorted(tickers), score, signals


def save_posts(db, payload):
    users = {str(u["id"]): u for u in payload.get("includes", {}).get("users", [])}
    added = 0
    for post in payload.get("data", []):
        post_id = str(post.get("id", ""))
        if not post_id.isdigit():
            raise CollectorError("X returned a post without a numeric ID; cursor was not advanced.")
        author_id = str(post.get("author_id", ""))
        username = users.get(author_id, {}).get("username", "")
        note = post.get("note_post") or post.get("note_tweet") or {}
        text = note.get("text") or post.get("text", "")
        entities = note.get("entities") or post.get("entities") or {}
        tickers, score, signals = analyse(text, entities)
        url = (f"https://x.com/{username}/status/{post_id}" if re.fullmatch(r"\w{1,50}", username)
               else f"https://x.com/i/web/status/{post_id}")
        row = (post_id, post.get("created_at", ""), author_id, username, text,
               json.dumps(tickers), score, json.dumps(signals), url,
               json.dumps(post, ensure_ascii=False))
        result = db.execute("INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        added += result.rowcount
    return added


def collect(db, client, query, max_posts=100, days=3, legacy_fields=False,
            start_time=None, end_time=None, cursor_key=None, fixed_window=False):
    cursor_key = cursor_key or query
    row = db.execute("SELECT state FROM cursors WHERE query=?", (cursor_key,)).fetchone()
    state = json.loads(row["state"]) if row else {"since_id": None, "active": False}
    if fixed_window and row and not state["active"]:
        return {"received": 0, "new": 0, "complete": True}
    if not state["active"]:
        state = {
            "since_id": state["since_id"], "active": True, "next_token": None,
            "high_id": state["since_id"], "end_time": end_time or stamp(utcnow() - timedelta(seconds=30)),
            "start_time": (None if state["since_id"] else
                           start_time or stamp(utcnow() - timedelta(days=days) + timedelta(seconds=120))),
        }
    oldest = utcnow() - timedelta(days=7)
    if parse_stamp(state["end_time"]) <= oldest or (
        state.get("start_time") and parse_stamp(state["start_time"]) <= oldest
    ):
        raise CollectorError(
            "Saved pagination is outside recent search's moving 7-day window. Use "
            "--reset-cursor to start afresh. Older missing posts require full-archive access."
        )
    received, added = 0, 0
    while max_posts - received >= getattr(client, "minimum_page", 10):
        fields = "created_at,author_id,public_metrics,entities,conversation_id,"
        params = {
            "query": query, "max_results": min(100, max_posts - received),
            "sort_order": "recency", "end_time": state["end_time"],
            "expansions": "author_id", "user.fields": "username",
            ("tweet.fields" if legacy_fields else "post.fields"):
                fields + ("note_tweet" if legacy_fields else "note_post"),
        }
        for key in ("since_id", "start_time", "next_token"):
            if state.get(key):
                params[key] = state[key]
        payload = client.get(params)
        posts = payload.get("data", [])
        if not isinstance(posts, list):
            raise CollectorError("Unexpected X data format; pagination was not advanced.")
        returned_count = payload.get("_returned_count", len(posts))
        if not isinstance(returned_count, int) or returned_count < len(posts):
            raise CollectorError("Invalid returned-post count; pagination was not advanced.")
        if payload.get("errors") and not posts:
            raise CollectorError("X returned errors without posts; pagination was not advanced.")
        if payload.get("errors"):
            log("X returned partial errors; delivered posts will be saved.")
        next_token = payload.get("meta", {}).get("next_token")
        if next_token and next_token == state.get("next_token"):
            raise CollectorError("X repeated a pagination token; stopping to avoid a read loop.")
        # Posts and their checkpoint commit together, so an interruption cannot skip a page.
        with db:
            new_count = save_posts(db, payload)
            ids = [int(p["id"]) for p in posts]
            if state.get("high_id"):
                ids.append(int(state["high_id"]))
            state["high_id"] = str(max(ids)) if ids else None
            state["next_token"] = next_token
            state["active"] = bool(next_token)
            if not next_token:
                # Advance the high-water mark ONLY when all pages are exhausted.
                state["since_id"] = state["high_id"]
            db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (cursor_key, json.dumps(state)))
        received += returned_count  # Includes duplicates and locally filtered results.
        added += new_count
        log(f"Page: {returned_count} returned, {new_count} new; run total {received}/{max_posts}.")
        if not next_token:
            return {"received": received, "new": added, "complete": True}
    log("Post cap reached (or fewer than one minimum page remains). Rerun to resume.")
    return {"received": received, "new": added, "complete": False}


def csv_safe(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def markdown_safe(value):
    value = html.escape(" ".join(str(value).split()))
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~])", r"\\\1", value)


def export_reports(db, folder, report_days=7, min_score=3):
    rows = db.execute("SELECT * FROM posts WHERE created_at>=? ORDER BY created_at DESC",
                      (stamp(utcnow() - timedelta(days=report_days)),)).fetchall()
    # Raw exports retain every locally saved post, including low-scoring posts.
    with (folder / "posts.jsonl").open("w", encoding="utf-8") as output:
        for row in db.execute("SELECT * FROM posts ORDER BY created_at DESC"):
            post = dict(row)
            for key in ("tickers", "signals", "raw_json"):
                post[key] = json.loads(post[key])
            output.write(json.dumps(post, ensure_ascii=False) + "\n")
    groups = defaultdict(list)
    seen_text = set()
    for row in rows:
        key = re.sub(r"\s+", " ", row["text"]).strip().casefold()
        if key in seen_text or row["evidence_score"] < min_score:
            continue
        seen_text.add(key)
        for ticker in json.loads(row["tickers"]):
            groups[ticker].append(row)
    ranked = sorted(groups, key=lambda t: (
        -max(p["evidence_score"] for p in groups[t]),
        -len({p["author_id"] for p in groups[t] if p["author_id"]}), -len(groups[t]), t,
    ))
    columns = ["ticker", "posts", "distinct_accounts", "evidence_score", "latest_utc",
               "signals", "example_url", "example_text"]
    with (folder / "stock_ideas.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(columns)
        for ticker in ranked:
            posts = groups[ticker]
            best = max(posts, key=lambda p: (p["evidence_score"], p["created_at"]))
            writer.writerow([csv_safe(v) for v in (
                ticker, len(posts), len({p["author_id"] for p in posts if p["author_id"]}),
                best["evidence_score"], max(p["created_at"] for p in posts),
                "; ".join(json.loads(best["signals"])), best["url"], best["text"],
            )])
    lines = ["# X stock research leads", "", f"Generated: {stamp(utcnow())}", "",
             f"Posts from the last {report_days:g} days; minimum evidence score: {min_score}.", "",
             "Scores count research-related words, not truth, account credibility or expected returns. "
             "Bullish and bearish posts can both rank highly. Verify the original posts and filings. "
             "Distinct accounts can repeat the same source; the sample is incomplete.", ""]
    for ticker in ranked:
        posts = groups[ticker]
        lines += [f"## {ticker}", "", f"{len(posts)} qualifying posts; "
                  f"{len({p['author_id'] for p in posts if p['author_id']})} distinct accounts.", ""]
        for post in sorted(posts, key=lambda p: (p["evidence_score"], p["created_at"]), reverse=True)[:3]:
            label = markdown_safe(post["username"] or post["author_id"] or "author unavailable")
            lines += [f"- [{label}]({post['url']}) — {post['created_at']} — "
                      f"evidence score {post['evidence_score']}", "",
                      "  " + markdown_safe(post["text"][:650]) + ("…" if len(post["text"]) > 650 else ""), ""]
    if not ranked:
        lines += ["No qualifying cashtagged posts in the saved sample. Try a different query "
                  "or a lower --min-score; neither proves a lack of investment opportunities.", ""]
    (folder / "stock_ideas.md").write_text("\n".join(lines), encoding="utf-8")
    return len(ranked)


def demo_payload():
    # Fictional posts and IDs: exercise the pipeline without market claims or API calls.
    texts = [
        "$DEMO bull case: valuation depends on free cash flow and margin recovery. "
        "The earnings catalyst is next quarter; the key risk is customer concentration.",
        "$DEMO bear case: debt and dilution could offset revenue growth. "
        "Review the balance sheet before accepting the investment thesis.",
        "$TEST undervalued? My thesis depends on a contract catalyst and backlog "
        "converting to revenue. Competition and weaker margins are the downside risks.",
        "$HYPE guaranteed returns 100x! Join my telegram pump group.",
    ]
    return {"data": [{"id": str(990000 + i), "author_id": str(800 + i),
                      "created_at": stamp(utcnow() - timedelta(hours=i + 1)), "text": text}
                     for i, text in enumerate(texts)],
            "includes": {"users": [{"id": str(800 + i), "username": f"fictional_demo_{i}"}
                                    for i in range(len(texts))]}, "meta": {"result_count": len(texts)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    search = parser.add_mutually_exclusive_group()
    search.add_argument("--query", help="Custom X search query (use single quotes around $tickers in bash).")
    search.add_argument("--tickers", nargs="+", help="Search specific cashtags, e.g. NVDA AMD MRVL.")
    parser.add_argument("--env-file", "--credentials", dest="credentials", type=Path, default=Path(__file__).resolve().parent / ".env")
    parser.add_argument("--out", type=Path, default=Path("x_stock_output"))
    parser.add_argument("--days", type=float, default=3, help="Initial search lookback, 0.01–7 days; default 3.")
    parser.add_argument("--max-posts", type=int, default=100, help="Cap on returned posts per run, including duplicates.")
    parser.add_argument("--max-requests", type=int, default=10, help="Cap on HTTP attempts per run, including retries.")
    parser.add_argument("--max-wait", type=float, default=900, help="Total allowed waiting seconds per run.")
    parser.add_argument("--report-days", type=float, default=7, help="Age cutoff for the shortlist, not raw exports.")
    parser.add_argument("--min-score", type=int, default=3, help="Heuristic research-word threshold for the shortlist.")
    parser.add_argument("--reset-cursor", action="store_true", help="Restart this query's search; retain saved posts.")
    parser.add_argument("--legacy-fields", action="store_true", help="Use older tweet.fields/note_tweet parameter names.")
    parser.add_argument("--export-only", action="store_true", help="Regenerate reports without network access.")
    parser.add_argument("--demo", action="store_true", help="Use fictional posts; no credentials or API calls.")
    args = parser.parse_args(argv)
    if not 0.01 <= args.days <= 7 or args.report_days <= 0 or not math.isfinite(args.report_days):
        parser.error("--days must be 0.01–7 and --report-days must be positive and finite.")
    if args.max_posts < 10 or args.max_requests < 1 or args.max_wait < 0 or not math.isfinite(args.max_wait):
        parser.error("--max-posts must be >=10, --max-requests >=1, and --max-wait finite and >=0.")
    query = args.query or DEFAULT_QUERY
    if args.tickers:
        tickers = [t.removeprefix("$").upper() for t in args.tickers]
        if any(not TICKER.fullmatch(t) for t in tickers):
            parser.error("Invalid ticker. Use symbols such as NVDA, AMD, BRK.B or SIVE.ST.")
        query = "(" + " OR ".join("$" + t for t in tickers) + ") " + (
            '(thesis OR catalyst OR valuation OR earnings OR margin OR revenue) '
            'lang:en -is:retweet -is:reply'
        )
    if not query.strip() or len(query) > 512:
        parser.error("Use a nonempty query of at most 512 characters (conservative standard-tier limit).")
    args.out.mkdir(parents=True, exist_ok=True)
    if args.demo:
        args.out = args.out / "demo"
        args.out.mkdir(exist_ok=True)
    db = open_db(args.out / "posts.sqlite3")
    exit_code = 0
    try:
        if args.demo:
            with db:
                save_posts(db, demo_payload())
            log("DEMO: fictional posts; zero API requests. Demo output is isolated in the demo subfolder.")
        elif not args.export_only:
            token = load_token(args.credentials)
            if args.reset_cursor:
                with db:
                    db.execute("DELETE FROM cursors WHERE query=?", (query,))
            client = XClient(token, args.max_requests, args.max_wait)
            log(f"Query: {query}\nCaps: {args.max_posts} returned posts; {args.max_requests} HTTP attempts.")
            result = collect(db, client, query, args.max_posts, args.days, args.legacy_fields)
            log(f"Finished: {result}; HTTP attempts: {client.requests}.")
    except PauseCollection as exc:
        log(f"Paused: {exc}")
        exit_code = 2
    except (CollectorError, OSError) as exc:
        log(f"Stopped: {exc}")
        exit_code = 1
    except KeyboardInterrupt:
        log("Interrupted. Committed pages are saved; rerun the same command to resume.")
        exit_code = 130
    finally:
        try:
            count = export_reports(db, args.out, args.report_days, args.min_score)
            log(f"Exported {count} ticker research leads to {args.out.resolve() / 'stock_ideas.csv'}")
        finally:
            db.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
