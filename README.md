# X stock and theme research with Claude

The default interpreter is now **Anthropic Claude**, configured through a local
`.env` beside the scripts. The defaults match the attached architecture package:
`https://lum.id/claude` and `claude-sonnet-5-5`. Use the endpoint and model your
provider authorizes. This gateway is separate from Google's Gemini free tier;
Claude availability, pricing and quotas depend on your provider/account.

## Quick start

```bash
python3 -m pip install -r requirements-free.txt
cp .env.example .env
```

Fill in `.env` locally:

```text
ANTHROPIC_BASE_URL=https://lum.id/claude
ANTHROPIC_AUTH_TOKEN=your_token
ANTHROPIC_MODEL=claude-sonnet-5-5
X_USERNAME=your_username
X_EMAIL=your_email
X_PASSWORD="your_password"
```

Then run `python3 discover.py`. The code sends Anthropic Messages-compatible
requests to `<base_url>/v1/messages`; a base URL already ending in `/v1` is also
supported. Like the reference package's `Anthropic(api_key=token, base_url=...)`,
the auth token is passed in `x-api-key`. Only HTTPS endpoints are accepted and
authenticated redirects are disabled. No token is stored in reports or URLs.

`.env` is gitignored. Shell environment values override the file; values remain
literal without shell execution or variable expansion. `--env-file` selects another
file; `--credentials` remains a backward-compatible alias. A valid own-session
cookie file can replace the X password via `--cookies`. The Gemini backend remains
available explicitly through `--analysis gemini`, with `GEMINI_API_KEY` and optional
`GEMINI_MODEL` in `.env`.

The first-three-sentence snippets, full-source preservation, evidence validation,
company directory, theme registry, caches and discovery metrics are shared by both
providers. Claude requests require one valid JSON object; malformed/truncated
responses are rejected and left queued. No alternative provider is used as a fallback.

Outputs are in `x_research_output/`. `--analyze-only` processes saved pending inputs;
`--export-only` rebuilds reports without network calls; `--demo` runs fictional data
through a deterministic local stub without any authentication or model calls.
`--max-ai-requests`, `--ai-rpm` and `--ai-max-wait` control the shared request limits;
the old Gemini-named flags are aliases. The reference package itself is unchanged.

The sections below describe the shared pipeline; Gemini-specific API/quota details
apply only when that optional backend is selected.

## Selected sources

`accounts.txt` contains the ten handles from the Investopedia article discussed:
CNBC, Benzinga, Stocktwits, BreakoutStocks, bespokeinvest, WSJMarkets,
Stephanie_Link, nytimesbusiness, IBDinvestors and WSJDealJournal.

Edit this plain-text file to add/remove sources. Account activity and handles can
change; the list is a starting point, not a verified list of currently active feeds.
The collector searches **posts authored by each handle**, in English, excluding
retweets and replies. It does not search every mention of the accounts. There is no
fixed stock watchlist and no thesis-keyword requirement in this curated mode.

Each author gets an allocation of the shared collection budget. Defaults are 200
returned posts and 20 X request attempts per run. With ten authors, Twikit requests
one 20-post allocation per account. Sparse accounts may return fewer; busy accounts
may have more posts than their allocation covers. `account_stats.csv` shows the
actual saved counts, not an estimate of each author's complete output.

The free source's internal page size may be advisory. An oversized response can
slightly overshoot a requested post allocation. All returned results, including
locally discarded results, count toward collection progress. Retries/login attempts
count toward the request-attempt cap. No official X API charges apply to Twikit.

## Text and token reduction

For each source post the program keeps:

- The complete original text and available metadata in SQLite/JSONL.
- Up to **three readable sentences**, capped at **600 characters**, for Gemini.
- All extracted cashtags separately, including cashtags after the third sentence.
- Original X URLs and available linked-page URLs, stored outside the AI prompt.

URLs and standalone promotional footers are removed from the snippet. Articles,
prepositions, negation and other meaningful sentence structure are retained; this
is not a stop-word-stripping step. The splitter handles common abbreviations,
decimals and dotted ticker codes heuristically; unusual punctuation can still need
review. Link-only/image-only posts are flagged locally as needing context, without
spending a Gemini request or leaving an endless pending queue.

Exact normalized duplicate snippets with the same cashtags share one interpretation.
Changing an excerpt, model or prompt version generates a new cache key. Dynamic
changes to the theme registry do not invalidate all previous work. Different
headlines about the same story may still require separate interpretation: this is
not guaranteed semantic deduplication of all syndicated stories.

The interpreter receives small application-level batches (default **10 distinct inputs**).
Gemini uses ordinary `generateContent`; Claude uses Anthropic Messages. Each full
request, including system instructions, schema and theme context, is bounded by
**14,000 characters** by default; batches shrink to fit. A character bound is not an
exact token bound. Provider-returned input/output/thinking/total token counts are
recorded in `gemini_usage` and summarized in `ai_status.json`.

## AI interpretation and evidence

The model returns structured JSON for each snippet:

- Relevance to investing.
- Explicitly mentioned companies, candidate tickers, source mention and evidence.
- Specific themes and subthemes, with confidence and supporting excerpts.
- A brief description of the reported event/claim.
- Bullish, bearish, neutral or unclear stance.
- A flag for missing context.

It is instructed to treat posts as untrusted data, ignore embedded instructions,
leave uncertainty unresolved and avoid inventing beneficiaries. Quotes must match
provided source text/cashtags after whitespace/case normalization. Unsupported
company/theme quotes are discarded and flagged for context. Missing/duplicate post
IDs, malformed JSON, or unknown registry IDs stop the batch without caching it.
Completed earlier batches remain saved. These checks establish text support, not
whether the author or model's interpretation is factually correct.

Low-confidence company/theme extractions do not enter the discovery ranking. Model
confidence labels are qualitative, not calibrated probabilities. Full posts remain
available for manual follow-up; this version flags unclear snippets rather than
automatically fetching linked articles or performing a second paid analysis pass.

## Company names, bare codes and aliases

At first live interpretation, the program attempts to cache Nasdaq Trader's public
US Nasdaq-listed and other-exchange symbol files. The reference refreshes after a
day or with `--refresh-symbols`. Test issues and ETFs are excluded; this is a limited
US security reference, not a worldwide or perfectly filtered common-stock universe.

Gemini can extract `Nvidia`, `NVDA`, `$NVDA` or `NASDAQ:NVDA` from the text. The local
reference then checks the proposed name/symbol mapping. A source company-name match
can correct an inconsistent model ticker. Ambiguous company names/share classes or
ambiguous word-like codes are left unresolved when evidence is inadequate.

Mapping notes distinguish direct reference matches, model-suggested brand aliases,
unverified explicit symbols and unresolved cases. A listed symbol alone does not
prove that an ambiguous brand alias was resolved correctly. Non-US/new/unmatched
companies may remain unresolved. If reference download fails, available cached
matches are used and unsupported candidates stay flagged.

Use a custom or expanded reference with:

```bash
python3 discover.py --company-map /path/to/companies.csv
```

Its header is `ticker,name,aliases`; separate aliases with semicolons. A market-wide
reference allows broad discovery; it need not be a selected investment watchlist.
Use `--offline-symbols` to avoid downloading a reference. Gemini and X will still
make their normal requests unless you also choose an offline mode.

## Themes, history and the meaning of new

Gemini suggests canonical themes and subthemes, expands unambiguous abbreviations,
and receives a compact registry of up to 40 recent persistent theme identities.
Matching narratives can reuse an existing ID even if worded differently. Existing
IDs and their stored labels remain stable across batches/runs. Semantic grouping is
model-suggested and imperfect: synonyms can still split, related themes can be
misgrouped, and older registry entries may be absent from the compact context.

Code, not the model, calculates novelty and attention changes. Defaults compare the
last **24 hours** with the **preceding 6 days** of qualifying interpreted posts.

| Label | Meaning |
| --- | --- |
| `new_in_sample` | No qualifying baseline mention or earlier qualifying interpreted mention in saved history. |
| `rising_in_sample` | Share of qualifying sampled posts is at least 2x its previous share. |
| `returning_in_sample` | Absent from the baseline, but present in older interpreted history. |
| `insufficient_baseline` | Too little history, or saved posts in the comparison window still await interpretation. |
| `established_in_sample` | Previously present without a 2x increase; hidden unless `--include-established` is set. |

Attention lift = `(recent mentions / all recent qualifying posts)` divided by
`(baseline mentions / all baseline qualifying posts)`. A zero baseline produces a
blank lift. A baseline needs at least 20 qualifying posts and some data at least
24 hours before the recent window. Pending AI interpretations in the comparison
window withhold novelty/momentum labels rather than treating missing analysis as
zero historical mentions.

At least two distinct accounts are required per candidate. Exact copied full text
is counted once within each window, so copied posts cannot alone establish breadth.
Distinct accounts are not necessarily independent or credible sources. Candidates
rank by signal group, account count, share lift, then post count. Company/theme
co-mentions are not a claim that every company benefits from the theme.

New means new in the interpreted collected sample, not a new listing, a globally
new idea, or an attractive investment. Caps, indexing delays, sparse/inactive feeds,
missed images, truncation and uneven collection leave gaps. Maintain consistent
sources when comparing windows. Changing accounts or source queries requires a
new `--out` folder so incompatible samples are not silently mixed.

Normal discovery refreshes the latest snapshot rather than getting stuck paging
through an old busy feed. Add `--resume` to continue unfinished live pages. Saved
posts and completed watermarks are retained. Historical seed windows resume their
own checkpoints. Moving seven-day limits and expired cursors can still prevent
recovery of old missing posts; `--reset-cursor` starts a new sample while keeping data.

## Free quota, resuming and offline modes

Default AI controls are **20 HTTP attempts/run**, target **4 requests/minute**,
**200 uncached inputs/run**, and **900 seconds of total pacing/retry waits**. These
are conservative application settings, not Google's promised free-tier allowance.
Your actual model/project quotas are visible in AI Studio and may be lower.

Per-minute limits respect server retry delays with bounded retries. Recognized daily
or zero-quota errors stop immediately. Authentication/model-access errors stop rather
than switching keys/models or enabling billing. Network failures do not automatically
replay uncertain generations. Reruns skip valid cached interpretations.

Finish saved pending work without collecting X again:

```bash
python3 discover.py --analyze-only
```

Rebuild reports with no X, Gemini or symbol-reference network calls, and no keys:

```bash
python3 discover.py --export-only
```

Run a completely offline demonstration without installing Twikit or using keys:

```bash
python3 discover.py --demo
```

The demo uses **fictional posts and a deterministic interpreter stub**, not live
Gemini. Outputs are isolated under `x_research_output/demo/` and labelled fictional.
It tests the pipeline; it does not demonstrate real model extraction quality.

You can accumulate baseline history through later runs. An optional initial backfill
across ten authors needs a larger collection budget (up to 1,600 posts for all date
slices), and the Gemini queue may take several quota windows to process:

```bash
python3 discover.py --seed-baseline --max-posts 1600 --max-requests 100
python3 discover.py --analyze-only
```

These commands do not schedule anything. Initial backfill is optional; do not infer
absence of a narrative from an incomplete history. Free-tier rate limits are per
project, and daily quotas reset at midnight Pacific time according to Google.

## Outputs

| File | Contents |
| --- | --- |
| `discovery_report.md` | Readable stock/theme shortlist, source excerpts, interpreted claims, stance and mapping caveats. |
| `emerging_stocks.csv` / `emerging_themes.csv` | Candidate metrics, novelty status, reference/mapping notes and source URLs. |
| `interpreted_posts.jsonl` | Structured interpretations with evidence, snippets, uncertainty and source links. |
| `ai_status.json` | Model, cached/pending/unreadable counts and cumulative returned token usage. |
| `account_stats.csv` | Actual saved counts per author, including the last 24 hours; incomplete collection is not a census. |
| `discovery.json` | Full candidate metrics and analysis coverage. |
| `posts.jsonl` / `posts.sqlite3` | Full saved posts, metadata, checkpoints, analysis cache, theme registry and usage ledger. |
| `company_directory.csv` | Cached US listing reference, when available. |

The original keyword-score `stock_ideas.csv`/`.md` exports remain available as legacy
outputs; use the discovery report for Gemini results. CSV cells and report text are
escaped against spreadsheet formulas and Markdown markup respectively.

## Legacy and advanced options

Run the old broad keyword/phrase workflow without Gemini:

```bash
python3 discover.py --analysis rules --broad-search --out legacy_research
```

The original official X API collector also remains in `x_stock_ideas.py`. For either
script's `--source api` route, add `X_BEARER_TOKEN` to .env and use your X
developer app/access. This is a separate paid X service; a Gemini key does not replace
it. Recent search covers seven days; this package does not implement full-archive X
search. `--legacy-fields` supports older `tweet.fields`/`note_tweet` parameter names.

Useful tuning flags: `--model`, `--snippet-chars`, `--batch-size`,
`--max-prompt-chars`, `--max-ai-posts`, `--max-gemini-requests`, `--gemini-rpm`,
`--gemini-max-wait`, `--min-authors`, `--recent-hours`, `--baseline-days` and
`--include-established`. See `python3 discover.py --help` for all options.
Run one collector per output folder at a time.

Exit codes: 0 = completed or reached the X post allocation; 1 = configuration/access
or response-validation error; 2 = bounded pause/quota/request cap; 130 = interrupted.
Committed data remains available after handled errors or interruption. No additional
API calls are made from the report-export/finalization stage.

## Validation and official references

```bash
python3 -m unittest -q
python3 discover.py --demo
```

Tests use simulated responses and fictional data; they make no live API calls or
spend credits. Live X login and Gemini generation need your own credentials and
have not been authenticated/tested here.

- [Gemini API authentication](https://ai.google.dev/api)
- [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)
- [Gemini models and free-tier pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Twikit project](https://github.com/d60/twikit)
- [Nasdaq symbol directory definitions](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs)
- [X API rate limits](https://docs.x.com/x-api/fundamentals/rate-limits)
- [X API pricing](https://docs.x.com/x-api/getting-started/pricing)

## Diagnosing X collection errors

The collector now reports whether password login or saved-session search failed.
Generic Twikit exception details are shown after redacting known configuration
and cookie values. Typed access-error bodies remain omitted. A `run_status.json`
records failed/paused/completed status separately from discovery results.

Zero candidates after a collection failure do not establish a lack of ideas; no
source data may have reached Claude. If ordinary X login works but password-based
Twikit login fails, use an existing valid session from your own account:

```bash
python3 discover.py --cookies /path/to/x_cookies.json
```

Twikit expects a JSON mapping of cookie names to values, not a browser cookie-export
array. Its documented `save_cookies` method produces the supported mapping. Refresh
the session through normal login when necessary. Do not share passwords or cookie
files. The code changes improve diagnostics; they do not establish the cause of a
particular login failure or guarantee current Twikit compatibility.
