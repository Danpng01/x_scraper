#!/usr/bin/env python3
"""Discover stocks and investment narratives from selected X accounts using Claude.

Default live source: free Twikit; interpretation: Claude through your configured endpoint.
Run --demo without credentials, network access or third-party packages.
"""

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import x_stock_ideas as core


QUERIES = [
    '("starter position" OR "new position" OR "initiated a position" OR '
    '"under the radar" OR "emerging theme" OR "investment thesis") '
    'lang:en -is:retweet -is:reply',
    '(stocks OR equities OR investing OR "stock market") '
    '(theme OR thematic OR catalyst OR bottleneck OR "supply chain" OR "early innings") '
    'lang:en -is:retweet -is:reply',
]
FINANCE = re.compile(r"\b(stock|stocks|equities|investing|investment|valuation|earnings|portfolio|capex|hyperscalers)\b", re.I)
STOP = set("""
a an the and or but of to in on at by for from with without into over under than
as if is are was were be been being it its this that these those i me my we our
you your he she they their them us who what when where why how not no yes can
could should would will shall may might must do does did have has had all any
some more most much many very too also just still already only even now then
today tomorrow yesterday day days week weeks month months year years next last
new old first second big bigger small best great good bad real really latest
one two three four five six seven eight nine ten another other each per about
here there see look watch think know want need make get going getting worth
stock stocks market markets equity equities investing investment investor investors
trade trades trader traders trading buy bought buying sell sold selling position
positions starter initiated thesis case bull bullish bear bearish catalyst catalysts
theme themes thematic sector sectors company companies shares share price prices
target targets valuation valued undervalued overvalued earnings revenue margin
margins guidance upside downside risk risks growth grows growing quarter quarters
strong stronger opportunity opportunities mention mentions mentioned talk talking
thread read reads follow follows following like likes retweet retweets source sources
potential demand supply chain early innings really interesting interestingly
example sample fictional reference baseline recent normal continued continues
""".split())
STATUS_ORDER = {"new_in_sample": 0, "rising_in_sample": 1, "returning_in_sample": 2,
                "insufficient_baseline": 3, "established_in_sample": 4}


def theme_phrases(text):
    """Unsupervised adjacent 2/3-word candidates; deliberately not semantic claims."""
    text = re.sub(r"https?://\S+|@\w+|\$[A-Za-z0-9_.-]+", " ", text.lower())
    text = text.replace("-", " ")
    phrases = set()
    for sentence in re.split(r"[.!?;:\n]", text):
        tokens = re.findall(r"[a-z][a-z0-9]{1,24}", sentence)[:500]
        for size in (2, 3):
            for offset in range(len(tokens) - size + 1):
                words = tokens[offset:offset + size]
                if all(word not in STOP for word in words):
                    phrases.add(" ".join(words))
    return phrases


def universe_from_file(path):
    if path is None:
        return None
    result = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.split(",", 1)[0].strip().strip('"').upper().removeprefix("$")
        if core.TICKER.fullmatch(value) and value not in {"SYMBOL", "TICKER"}:
            result.add(value)
    if not result:
        raise core.CollectorError("The universe file contains no recognized ticker symbols.")
    return result


def analyse_discovery(db, recent_hours=24, baseline_days=6, min_authors=2,
                      min_baseline_posts=20, min_score=2, universe=None,
                      include_established=False, now=None, interpretations=None,
                      scope_accounts=None):
    now = now or core.utcnow()
    recent_start = now - timedelta(hours=recent_hours)
    baseline_start = recent_start - timedelta(days=baseline_days)
    pools = {"recent": [], "baseline": []}
    seen = {"recent": set(), "baseline": set()}
    first_seen = {"stocks": {}, "themes": {}}
    pending_window = 0
    for row in db.execute("SELECT * FROM posts ORDER BY created_at DESC"):
        try:
            date = core.parse_stamp(row["created_at"])
        except (ValueError, TypeError):
            continue
        if date > now or (scope_accounts and row["username"].casefold() not in scope_accounts):
            continue
        interpretation = interpretations.get(row["id"]) if interpretations is not None else None
        entity_notes = []
        if interpretations is not None:
            if interpretation is None:
                pending_window += int(date >= baseline_start)
                continue
            if not interpretation["relevant"]:
                continue
            companies = [c for c in interpretation["companies"] if c["confidence"] != "low"]
            tickers = {c["ticker"] for c in companies if c["ticker"]}
            phrases = {((t["parent"] + " / ") if t["parent"] and t["parent"] != t["label"] else "") + t["label"]
                       for t in interpretation["themes"] if t["confidence"] != "low"}
            entity_notes = sorted({f"{c['ticker'] or c['name']}: {c['mapping_status']}" for c in companies
                                   if c["mapping_status"] not in {"reference_name", "reference_symbol"}})
        else:
            if row["evidence_score"] < min_score or core.PROMO.search(row["text"]):
                continue
            tickers = set(json.loads(row["tickers"]))
            if not tickers and not FINANCE.search(row["text"]):
                continue
            phrases = theme_phrases(row["text"])
        if universe is not None:
            tickers &= universe
        for kind, entities in (("stocks", tickers), ("themes", phrases)):
            for entity in entities:
                previous = first_seen[kind].get(entity)
                if previous is None or date < previous:
                    first_seen[kind][entity] = date
        window = "recent" if date >= recent_start else "baseline" if date >= baseline_start else None
        if window is None:
            continue
        # Retain the same wording in different windows: removing it globally could
        # erase a historical mention and incorrectly label it new.
        copied_text = re.sub(r"\s+", " ", row["text"]).strip().casefold()
        if copied_text in seen[window]:
            continue
        seen[window].add(copied_text)
        document = dict(row)
        document.update({"date": date, "stocks": tickers, "themes": phrases})
        document["entity_notes"] = entity_notes
        if interpretation is not None:
            document.update({"event": interpretation["event"], "stance": interpretation["stance"],
                             "needs_context": interpretation["needs_context"],
                             "snippet_truncated": interpretation["snippet_truncated"]})
        pools[window].append(document)
    enough_age = (bool(pools["baseline"]) and
                  min(p["date"] for p in pools["baseline"]) <=
                  recent_start - timedelta(hours=min(24, baseline_days * 12)))
    baseline_ready = len(pools["baseline"]) >= min_baseline_posts and enough_age and pending_window == 0
    metrics = {"recent_start_utc": core.stamp(recent_start),
               "baseline_start_utc": core.stamp(baseline_start),
               "recent_qualifying_posts": len(pools["recent"]),
               "baseline_qualifying_posts": len(pools["baseline"]),
               "baseline_ready": bool(baseline_ready),
               "minimum_distinct_accounts": min_authors,
               "analysis_mode": "gemini" if interpretations is not None else "rules",
               "pending_interpretations_in_window": pending_window}
    output = {}
    for kind in ("stocks", "themes"):
        mentions = {"recent": defaultdict(list), "baseline": defaultdict(list)}
        for window, documents in pools.items():
            for document in documents:
                for entity in document[kind]:
                    mentions[window][entity].append(document)
        rows = []
        for entity, recent in mentions["recent"].items():
            authors = {p["author_id"] for p in recent if p["author_id"]}
            if len(authors) < min_authors:
                continue
            baseline = mentions["baseline"].get(entity, [])
            recent_share = len(recent) / max(1, len(pools["recent"]))
            prior_share = len(baseline) / max(1, len(pools["baseline"]))
            lift = recent_share / prior_share if prior_share else None
            earliest = first_seen[kind][entity]
            if not baseline_ready:
                status = "insufficient_baseline"
            elif not baseline:
                status = "new_in_sample" if earliest >= recent_start else "returning_in_sample"
            elif lift >= 2:
                status = "rising_in_sample"
            else:
                status = "established_in_sample"
            if status == "established_in_sample" and not include_established:
                continue
            samples, sampled_authors = [], set()
            for post in sorted(recent, key=lambda p: (p["evidence_score"], p["created_at"]), reverse=True):
                if post["author_id"] in sampled_authors:
                    continue
                sampled_authors.add(post["author_id"])
                samples.append({"url": post["url"], "text": post["text"],
                                "created_at": post["created_at"], "username": post["username"],
                                "event": post.get("event", ""), "stance": post.get("stance", ""),
                                "needs_context": post.get("needs_context", False)})
                if len(samples) == 3:
                    break
            related = Counter(ticker for post in recent for ticker in post["stocks"])
            rows.append({"candidate": entity, "status": status,
                         "distinct_accounts": len(authors), "recent_posts": len(recent),
                         "baseline_posts": len(baseline), "recent_share": recent_share,
                         "baseline_share": prior_share, "attention_lift": lift if baseline_ready else None,
                         "earliest_saved_mention_utc": core.stamp(earliest),
                         "latest_mention_utc": max(p["created_at"] for p in recent),
                         "related_tickers": [t for t, _ in related.most_common(10)],
                         "entity_notes": sorted({note for p in recent for note in p["entity_notes"]}),
                         "samples": samples,
                         "_post_ids": {p["id"] for p in recent}})
        if kind == "themes" and interpretations is None:
            # Prefer a longer phrase when it explains at least 80% of the shorter
            # phrase's posts; retain a broader phrase with substantial extra support.
            redundant = set()
            for short in rows:
                if len(short["candidate"].split()) != 2:
                    continue
                for long in rows:
                    if (len(long["candidate"].split()) == 3 and
                        (" " + short["candidate"] + " ") in (" " + long["candidate"] + " ") and
                        len(short["_post_ids"] & long["_post_ids"]) >= 0.8 * len(short["_post_ids"])):
                        redundant.add(short["candidate"])
                        break
            rows = [row for row in rows if row["candidate"] not in redundant]
        rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], -r["distinct_accounts"],
                                 -(r["attention_lift"] or 0), -r["recent_posts"], r["candidate"]))
        for row in rows:
            del row["_post_ids"]
        output[kind] = rows
    output["metrics"] = metrics
    return output


def write_discovery(folder, report, recent_hours, baseline_days, source, demo=False):
    fields = ["candidate", "status", "distinct_accounts", "recent_posts", "baseline_posts",
              "recent_share", "baseline_share", "attention_lift", "earliest_saved_mention_utc",
              "latest_mention_utc", "related_tickers", "entity_notes", "source_urls"]
    for kind, filename in (("stocks", "emerging_stocks.csv"), ("themes", "emerging_themes.csv")):
        with (folder / filename).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in report[kind]:
                exported = {key: row.get(key, "") for key in fields}
                exported["attention_lift"] = "" if row["attention_lift"] is None else round(row["attention_lift"], 3)
                exported["related_tickers"] = ";".join(row["related_tickers"])
                exported["entity_notes"] = "; ".join(row["entity_notes"])
                exported["source_urls"] = ";".join(p["url"] for p in row["samples"])
                writer.writerow({key: core.csv_safe(value) for key, value in exported.items()})
    (folder / "discovery.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    metrics = report["metrics"]
    lines = ["# Emerging stocks and themes", "", f"Generated: {core.stamp(core.utcnow())}; source: {source}.", ""]
    if demo:
        lines += ["**Fictional demonstration data. These are not live X findings.**", ""]
    lines += [f"Recent window: {recent_hours:g} hours; baseline: the preceding {baseline_days:g} days.", "",
              f"Qualifying sampled posts: {metrics['recent_qualifying_posts']} recent, "
              f"{metrics['baseline_qualifying_posts']} baseline. Baseline ready: {metrics['baseline_ready']}.", "",
              "New means new in this collected sample, not a new listing or first mention anywhere on X. "
              "Rising means at least 2x its previous share of qualifying sampled posts. "
              "Distinct accounts are not necessarily independent or credible sources. " +
              ("Themes are AI interpretations with source evidence, not verified investment facts."
               if metrics.get("analysis_mode") in {"gemini", "claude"} else
               "Theme phrases are automatic text candidates requiring review, not verified investment themes."), ""]
    if metrics.get("pending_interpretations_in_window"):
        lines += [f"**Analysis incomplete:** {metrics['pending_interpretations_in_window']} saved posts in the comparison window "
                  "are not interpreted yet. Novelty/momentum labels are withheld until this queue is processed.", ""]
    if not metrics["baseline_ready"]:
        lines += ["**Insufficient baseline:** candidates are newly observed leads only. Collect more history "
                  "or use --seed-baseline before interpreting novelty or momentum.", ""]
    theme_label = "Investment theme candidates" if metrics.get("analysis_mode") in {"gemini", "claude"} else "Theme phrase candidates"
    for kind, label in (("stocks", "Ticker candidates"), ("themes", theme_label)):
        lines += [f"## {label}", "", "| Candidate | Signal | Accounts | Recent posts | Baseline posts | Share lift |",
                  "| --- | --- | ---: | ---: | ---: | ---: |"]
        for row in report[kind][:25]:
            lift = f"{row['attention_lift']:.1f}x" if row["attention_lift"] is not None else "—"
            lines.append(f"| {core.markdown_safe(row['candidate'])} | {row['status'].replace('_', ' ')} | "
                         f"{row['distinct_accounts']} | {row['recent_posts']} | {row['baseline_posts']} | {lift} |")
        if not report[kind]:
            lines += ["", "No candidates meet the current account and research-word thresholds."]
        lines += [""]
        for row in report[kind][:15]:
            lines += [f"### {core.markdown_safe(row['candidate'])}", "",
                      f"Earliest qualifying saved mention: {row['earliest_saved_mention_utc']}.", ""]
            if kind == "themes":
                lines += ["Co-mentioned ticker candidates: " + (", ".join(row["related_tickers"]) or "none"), ""]
            if row["entity_notes"]:
                lines += ["Mapping notes: " + core.markdown_safe("; ".join(row["entity_notes"])), ""]
            for sample in row["samples"]:
                name = core.markdown_safe(sample["username"] or "author")
                lines += [f"- [{name}]({sample['url']}) — {sample['created_at']}", "",
                          "  " + core.markdown_safe(sample["text"][:500]), ""]
                if sample.get("event"):
                    lines += ["  Interpreted claim: " + core.markdown_safe(sample["event"]) +
                              f"; stance: {sample['stance']}; needs context: {sample['needs_context']}.", ""]
    (folder / "discovery_report.md").write_text("\n".join(lines), encoding="utf-8")


def collection_jobs(queries, source, seed_baseline, recent_hours, baseline_days, budget, now=None):
    now = now or core.utcnow()
    recent_start = now - timedelta(hours=recent_hours)
    jobs = [{"query": q, "cursor_key": f"discovery:{source}:live:{q}",
             "start_time": core.stamp(recent_start), "end_time": core.stamp(now - timedelta(seconds=30)),
             "fixed_window": False, "weight": 3} for q in queries]
    if seed_baseline:
        start = max(recent_start - timedelta(days=baseline_days), now - timedelta(days=7) + timedelta(seconds=120))
        # Sample each calendar-date slice rather than just the newest baseline posts.
        while start < recent_start:
            midnight = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = min(midnight, recent_start)
            for query in queries:
                begin, finish = core.stamp(start), core.stamp(end)
                jobs.append({"query": query, "cursor_key": f"discovery:{source}:seed:{begin}:{finish}:{query}",
                             "start_time": begin, "end_time": finish, "fixed_window": True, "weight": 1})
            start = end
    quantum = 20 if source == "twikit" else 10
    minimum = quantum * len(jobs)
    if budget < minimum:
        raise core.CollectorError(f"This collection plan needs --max-posts at least {minimum} ({quantum} per query/window).")
    units, weights = (budget - minimum) // quantum, sum(job["weight"] for job in jobs)
    allocations = [int(units * job["weight"] / weights) for job in jobs]
    remainder = units - sum(allocations)
    priority = sorted(range(len(jobs)), key=lambda i: (-(units * jobs[i]["weight"] / weights - allocations[i]), i))
    for index in priority[:remainder]:
        allocations[index] += 1
    for job, allocation in zip(jobs, allocations):
        job["max_posts"] = quantum * (1 + allocation)
        del job["weight"]
    return jobs


def refresh_live_snapshot(db, key):
    """Keep completed watermarks, but prioritize fresh posts over unfinished old pages."""
    row = db.execute("SELECT state FROM cursors WHERE query=?", (key,)).fetchone()
    if row:
        state = json.loads(row["state"])
        if state["active"]:
            with db:
                db.execute("UPDATE cursors SET state=? WHERE query=?", (
                    json.dumps({"since_id": state["since_id"], "active": False}), key))


def seed_demo(db):
    now = core.utcnow()
    posts, users = [], []
    def add(symbol, phrase, hours, author, index):
        post_id = str(880000 + index)
        posts.append({"id": post_id, "created_at": core.stamp(now - timedelta(hours=hours)),
                      "author_id": str(author), "text": f"${symbol}: {phrase}. Investment thesis and valuation "
                      f"depend on revenue conversion; competition is the key risk. Fictional reference {index}."})
        users.append({"id": str(author), "username": f"fictional_{author}"})
    for i in range(40):
        add("BASE", "silicon photonics", 26 + i * 2.5, 100 + i, i)
    for i in range(2):
        add("RISE", "photon weaving", 60 + i * 10, 300 + i, 100 + i)
    for i in range(5):
        add("NEW", "orbital cooling", 1 + i, 400 + i, 200 + i)
    for i in range(4):
        add("RISE", "photon weaving", 2 + i, 500 + i, 300 + i)
    for i in range(2):
        add("BASE", "silicon photonics", 3 + i, 600 + i, 400 + i)
    with db:
        core.save_posts(db, {"data": posts, "includes": {"users": users}})


def load_accounts(path):
    accounts = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        handle = line.split("#", 1)[0].strip().removeprefix("@")
        if not handle:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise core.CollectorError(f"Invalid account handle in {path.name}.")
        if handle.casefold() not in {a.casefold() for a in accounts}:
            accounts.append(handle)
    if not accounts:
        raise core.CollectorError("The accounts file is empty.")
    return accounts


def export_account_stats(db, folder):
    recent = core.stamp(core.utcnow() - timedelta(hours=24))
    rows = db.execute("SELECT username, COUNT(*) AS saved_posts, SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS saved_last_24h, "
                      "MIN(created_at) AS earliest_saved, MAX(created_at) AS latest_saved FROM posts GROUP BY username", (recent,))
    with (folder / "account_stats.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["account", "saved_posts", "saved_last_24h", "earliest_saved_utc", "latest_saved_utc"])
        for row in rows:
            writer.writerow([core.csv_safe(value) for value in row])


def main(argv=None):
    import gemini_analysis as gemini
    import claude_analysis as claude
    from company_directory import CompanyDirectory

    parent = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["twikit", "api"], default="twikit")
    parser.add_argument("--query", action="append", help="Override discovery query; repeat for multiple queries.")
    parser.add_argument("--accounts-file", type=Path, default=Path(__file__).resolve().parent / "accounts.txt")
    parser.add_argument("--broad-search", action="store_true", help="Use the older broad keyword searches instead of selected authors.")
    parser.add_argument("--analysis", choices=["claude", "gemini", "rules"], default=None)
    parser.add_argument("--out", type=Path, default=Path("x_research_output"))
    parser.add_argument("--env-file", "--credentials", dest="credentials", type=Path, default=parent / ".env")
    parser.add_argument("--cookies", type=Path, default=parent / "x_cookies.json")
    parser.add_argument("--max-posts", type=int, default=200)
    parser.add_argument("--max-requests", type=int, default=20)
    parser.add_argument("--max-wait", type=float, default=900)
    parser.add_argument("--recent-hours", type=float, default=24)
    parser.add_argument("--baseline-days", type=float, default=6)
    parser.add_argument("--min-authors", type=int, default=2)
    parser.add_argument("--min-baseline-posts", type=int, default=20)
    parser.add_argument("--min-score", type=int, default=2)
    parser.add_argument("--universe", type=Path, help="Optional allowed equity symbols, one per line or first CSV column.")
    parser.add_argument("--include-established", action="store_true")
    parser.add_argument("--seed-baseline", action="store_true", help="Also collect date-stratified historical samples.")
    parser.add_argument("--reset-cursor", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Continue unfinished live pages instead of refreshing the newest snapshot.")
    parser.add_argument("--legacy-fields", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--analyze-only", action="store_true", help="Interpret saved posts with Gemini without fetching X.")
    parser.add_argument("--model", help="Gemini model ID; overrides GEMINI_MODEL in .env.")
    parser.add_argument("--snippet-chars", type=int, default=600)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-prompt-chars", type=int, default=14000)
    parser.add_argument("--max-ai-posts", type=int, default=200, help="Maximum distinct uncached snippets interpreted per run.")
    parser.add_argument("--max-ai-requests", "--max-gemini-requests", dest="max_gemini_requests", type=int, default=20, help="Maximum Gemini HTTP attempts, including retries.")
    parser.add_argument("--ai-rpm", "--gemini-rpm", dest="gemini_rpm", type=float, default=4)
    parser.add_argument("--ai-max-wait", "--gemini-max-wait", dest="gemini_max_wait", type=float, default=900)
    parser.add_argument("--company-map", type=Path, help="Optional CSV reference with ticker,name,aliases (semicolon-separated aliases).")
    parser.add_argument("--refresh-symbols", action="store_true")
    parser.add_argument("--offline-symbols", action="store_true", help="Use only cached company reference data.")
    args = parser.parse_args(argv)
    if (not math.isfinite(args.recent_hours) or not math.isfinite(args.baseline_days) or
        args.recent_hours < 1 or args.baseline_days < 1 or args.recent_hours + args.baseline_days * 24 > 168):
        parser.error("Use recent-hours >=1, baseline-days >=1, with a combined window of at most 7 days.")
    if args.max_posts < 20 or args.max_requests < 1 or args.min_authors < 2 or args.min_baseline_posts < 1:
        parser.error("Use max-posts >=20, max-requests >=1, min-authors >=2 and min-baseline-posts >=1.")
    if not math.isfinite(args.max_wait) or args.max_wait < 0:
        parser.error("--max-wait must be finite and nonnegative.")
    if (args.snippet_chars < 80 or not 1 <= args.batch_size <= 20 or args.max_prompt_chars < 5000 or
        args.max_ai_posts < 1 or args.max_gemini_requests < 1 or not math.isfinite(args.gemini_rpm) or
        args.gemini_rpm <= 0 or not math.isfinite(args.gemini_max_wait) or args.gemini_max_wait < 0):
        parser.error("Use snippet-chars >=80, batch-size 1–20, max-prompt-chars >=5000, positive AI caps/RPM, and finite nonnegative Gemini max-wait.")
    if args.export_only and args.analyze_only:
        parser.error("Choose --export-only or --analyze-only.")
    if args.demo:
        args.out = args.out / "demo"
    args.out.mkdir(parents=True, exist_ok=True)
    db = core.open_db(args.out / "posts.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS discovery_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    gemini.init_tables(db)
    previous_provider = db.execute("SELECT value FROM discovery_config WHERE key='ai_provider'").fetchone()
    args.analysis = args.analysis or (previous_provider["value"] if previous_provider and (args.export_only or args.analyze_only) else "claude")
    client, exit_code, universe = None, 0, None
    universe_checked = args.universe is None
    display_source = "fictional demo" if args.demo else args.source
    selected_accounts, queries = None, []
    model = "demo-gemini" if args.demo else args.model or (claude.DEFAULT_MODEL if args.analysis == "claude" else gemini.DEFAULT_MODEL)
    directory = CompanyDirectory()
    try:
        universe = universe_from_file(args.universe)
        universe_checked = True
        if not args.query and not args.broad_search:
            handles = load_accounts(args.accounts_file)
            selected_accounts = {h.casefold() for h in handles}
            queries = [f"from:{handle} lang:en -is:retweet -is:reply" for handle in handles]
        else:
            queries = list(dict.fromkeys(args.query or QUERIES))
        if any(not q.strip() or len(q) > 512 for q in queries):
            raise core.CollectorError("Each query must contain 1–512 characters.")
        previous = db.execute("SELECT value FROM discovery_config WHERE key='scope'").fetchone()
        previous_model = db.execute("SELECT value FROM discovery_config WHERE key='gemini_model'").fetchone()
        if (args.analyze_only or args.export_only) and previous:
            old_scope = json.loads(previous["value"])
            selected_accounts = set(old_scope.get("accounts") or []) or None
            display_source = old_scope["source"]
        if args.export_only and not args.model and previous_model:
            model = previous_model["value"]
        api_key = None
        if args.analysis in {"gemini", "claude"} and not args.demo and not args.export_only:
            if args.analysis == "claude":
                api_key, claude_base_url, configured_model = claude.load_settings(args.credentials)
            else:
                api_key, configured_model = gemini.load_settings(args.credentials)
            model = args.model or configured_model
        model = model.removeprefix("models/")
        if args.analysis == "gemini" and not args.demo and model != "demo-gemini" and not re.fullmatch(r"gemini-[a-z0-9.-]+", model):
            raise core.CollectorError("Invalid Gemini model ID.")
        if args.analysis in {"gemini", "claude"}:
            directory = CompanyDirectory.load(args.out, args.company_map, offline=True)
            with db:
                db.execute("INSERT OR REPLACE INTO discovery_config VALUES ('gemini_model',?)", (model,))
                db.execute("INSERT OR REPLACE INTO discovery_config VALUES ('ai_provider',?)", (args.analysis,))
        if args.demo:
            seed_demo(db)
            selected_accounts = None
            with db:
                db.execute("INSERT OR REPLACE INTO discovery_config VALUES ('scope',?)", (
                    json.dumps({"source": "fictional demo", "queries": [], "accounts": None}),))
            core.log("Fictional discovery demonstration: zero network requests.")
        elif not args.export_only and not args.analyze_only:
            scope = json.dumps({"source": args.source, "queries": sorted(queries),
                                "accounts": sorted(selected_accounts) if selected_accounts else None})
            if previous and previous["value"] != scope:
                raise core.CollectorError("Use a separate --out folder when changing source or queries, to keep the baseline comparable.")
            jobs = collection_jobs(queries, args.source, args.seed_baseline, args.recent_hours,
                                   args.baseline_days, args.max_posts)
            with db:
                db.execute("INSERT OR REPLACE INTO discovery_config VALUES ('scope',?)", (scope,))
                if args.reset_cursor:
                    db.execute("DELETE FROM cursors WHERE query LIKE ?", (f"discovery:{args.source}:%",))
            if args.source == "api":
                client = core.XClient(core.load_token(args.credentials), args.max_requests, args.max_wait)
            else:
                from twikit_source import TwikitSource
                client = TwikitSource(args.cookies, args.credentials, args.max_requests, args.max_wait)
            for index, job in enumerate(jobs, 1):
                core.log(f"Discovery query/window {index}/{len(jobs)}; allocation {job['max_posts']} returned posts.")
                if not job["fixed_window"] and not args.resume:
                    refresh_live_snapshot(db, job["cursor_key"])
                core.collect(db, client, legacy_fields=args.legacy_fields, **job)
        if args.analysis in {"gemini", "claude"} and not args.export_only:
            if args.demo:
                interpreter = gemini.DemoGemini()
            else:
                directory = CompanyDirectory.load(args.out, args.company_map,
                                                  refresh=args.refresh_symbols, offline=args.offline_symbols)
                interpreter = (claude.ClaudeClient(api_key, claude_base_url, model, args.max_gemini_requests,
                                                  args.gemini_rpm, args.gemini_max_wait, db=db)
                               if args.analysis == "claude" else
                               gemini.GeminiClient(api_key, model, args.max_gemini_requests,
                                                   args.gemini_rpm, args.gemini_max_wait, db=db))
            gemini.interpret_posts(db, interpreter, args.snippet_chars, args.batch_size,
                                   args.max_prompt_chars, args.max_ai_posts, selected_accounts)
    except core.PauseCollection as exc:
        core.log(f"Paused: {exc}")
        exit_code = 2
    except (core.CollectorError, OSError, ValueError) as exc:
        core.log(f"Stopped: {exc}")
        exit_code = 1
    except KeyboardInterrupt:
        core.log("Interrupted; committed pages are saved. Rerun the same command to resume.")
        exit_code = 130
    finally:
        try:
            # Preserve existing raw exports, and add the discovery-specific reports.
            core.export_reports(db, args.out)
            export_account_stats(db, args.out)
            saved_count = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            (args.out / "run_status.json").write_text(json.dumps({
                "status": "failed" if exit_code == 1 else "paused" if exit_code else "completed",
                "exit_code": exit_code, "saved_posts": saved_count,
            }, indent=2), encoding="utf-8")
            if exit_code and not saved_count:
                core.log("No X posts were saved. Empty reports reflect collection failure, not a lack of stocks or themes. AI interpretation has not run.")
            scope = db.execute("SELECT value FROM discovery_config WHERE key='scope'").fetchone()
            if scope and not args.demo:
                saved_scope = json.loads(scope["value"])
                display_source = saved_scope["source"]
                selected_accounts = set(saved_scope.get("accounts") or []) or None
            if universe_checked:
                interpretations, pending = None, 0
                if args.analysis in {"gemini", "claude"}:
                    interpretations, pending = gemini.cached_interpretations(db, model, directory,
                                                                            args.snippet_chars, selected_accounts)
                    gemini.export_interpretations(db, args.out, interpretations, pending, model)
                report = analyse_discovery(db, args.recent_hours, args.baseline_days, args.min_authors,
                                          args.min_baseline_posts, args.min_score, universe, args.include_established,
                                          interpretations=interpretations, scope_accounts=selected_accounts)
                report["metrics"]["analysis_mode"] = args.analysis
                write_discovery(args.out, report, args.recent_hours, args.baseline_days, display_source,
                                args.demo or display_source == "fictional demo")
                core.log(f"Discovery exported: {len(report['stocks'])} ticker candidates; "
                         f"{len(report['themes'])} phrase candidates. Baseline ready: {report['metrics']['baseline_ready']}.")
                if args.analysis in {"gemini", "claude"}:
                    core.log(f"AI cache: {len(interpretations)} interpreted posts; {pending} saved posts still pending.")
            else:
                core.log("Discovery report was not regenerated because the universe file is invalid.")
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()
            db.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
