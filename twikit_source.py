"""Optional free source adapter. No account rotation, proxying or challenge bypass."""

import asyncio
import re
from datetime import timedelta, timezone
from email.message import Message
from pathlib import Path

import x_stock_ideas as core


def read_login(path):
    from config import load_config
    values = load_config(Path(path))
    if not values.get("X_USERNAME") or not values.get("X_PASSWORD"):
        raise core.CollectorError("Set X_USERNAME and X_PASSWORD in .env, or supply --cookies.")
    return values


class TwikitSource(core.XClient):
    """Synchronous adapter retaining one event loop for Twikit's async HTTP client."""

    minimum_page = 20

    def __init__(self, cookies, credentials, max_requests=20, max_wait=900, client=None):
        super().__init__("", max_requests=max_requests, max_wait=max_wait, interval=3.0)
        if client is None:
            try:
                from twikit import Client
            except ImportError as exc:
                raise core.CollectorError("Install the free source with: python3 -m pip install -r requirements-free.txt") from exc
            client = Client("en-US")
        self.client = client
        self.loop = asyncio.new_event_loop()
        self.cookies = Path(cookies)
        self.secrets = set()
        from config import load_config
        self.secrets.update(value for value in load_config(Path(credentials)).values() if value)
        try:
            if self.cookies.is_file():
                core.log("X: loading saved session; next step is searching selected authors.")
                self.client.load_cookies(str(self.cookies))
            else:
                core.log("X: no saved session found; attempting password login.")
                login = read_login(credentials)
                self.cookies.parent.mkdir(parents=True, exist_ok=True)
                self.call(self.client.login, auth_info_1=login["X_USERNAME"],
                          auth_info_2=login.get("X_EMAIL"), password=login["X_PASSWORD"],
                          cookies_file=str(self.cookies), retry=False)
                if self.cookies.is_file():
                    self.cookies.chmod(0o600)
        except BaseException as exc:
            self.close()
            if isinstance(exc, (ValueError, TypeError)):
                raise core.CollectorError("The saved session file is invalid; supply your own valid X cookie file.") from None
            raise

    def safe_detail(self, exc):
        """Expose generic Twikit errors after removing known credentials/session values."""
        try:
            self.secrets.update(str(v) for v in self.client.get_cookies().values() if v)
        except Exception:
            pass
        detail = str(exc)
        for secret in sorted(self.secrets, key=len, reverse=True):
            detail = detail.replace(secret, "[REDACTED]")
        detail = re.sub(r"(?i)([\"']?(?:password|auth_token|ct0|authorization|token|api_key)[\"']?\s*[:=]\s*)[\"']?[^\s,;\"'}]+", r"\1[REDACTED]", detail)
        detail = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL REDACTED]", detail)
        detail = re.sub(r"https?://\S+", "[URL OMITTED]", detail)
        return " ".join(detail.split())[:450] or "No detail supplied by Twikit."

    def call(self, function, retry=True, **kwargs):
        for attempt in range(4 if retry else 1):
            if self.requests >= self.max_requests:
                raise core.PauseCollection("Reached the free source's request-attempt cap; rerun to resume.")
            self.wait(self.not_before - core.time.time(), "Free-source request pacing / reset")
            self.requests += 1
            try:
                value = self.loop.run_until_complete(function(**kwargs))
                self.not_before = core.time.time() + self.interval
                return value
            except Exception as exc:
                # Avoid printing exception bodies: login errors can contain sensitive session data.
                name = type(exc).__name__
                if name == "TooManyRequests" and retry and attempt < 3:
                    response_headers = Message()
                    for key, value in (getattr(exc, "headers", None) or {}).items():
                        response_headers[key] = str(value)
                    reset = getattr(exc, "rate_limit_reset", None)
                    if reset is not None and response_headers.get("x-rate-limit-reset") is None:
                        response_headers["x-rate-limit-reset"] = str(reset)
                    self.not_before = core.time.time() + core.retry_delay(response_headers, attempt)
                    core.log("X limited the free source; respecting the reset before retrying.")
                    continue
                if name in {"ReadTimeout", "ConnectTimeout", "ConnectError", "ServerError"} and retry and attempt < 3:
                    self.not_before = core.time.time() + min(2 ** (attempt + 1), 60)
                    continue
                if name == "TooManyRequests":
                    raise core.PauseCollection("Free-source rate limit persists; stop and retry later.") from None
                phase = "login" if getattr(function, "__name__", "") == "login" else "search"
                # Typed access errors can contain response bodies. Only expose the
                # generic exception text that was hidden in the previous release.
                detail = self.safe_detail(exc) if name in {"Exception", "TwitterException"} else "Access/session error; response body omitted."
                raise core.CollectorError(
                    f"X {phase} failed ({name}): {detail} "
                    "If normal X login works but automated login fails, use a valid saved session "
                    "from your own account with --cookies /path/to/x_cookies.json. "
                    "The session file must be Twikit's JSON cookie mapping. "
                    "Resolve any verification normally on X; this code cannot bypass it."
                ) from None

    def get(self, params):
        query = params["query"].replace("-is:retweet", "-filter:retweets").replace("-is:reply", "-filter:replies")
        start = core.parse_stamp(params["start_time"]) if params.get("start_time") else None
        end = core.parse_stamp(params["end_time"])
        # Website date filters are day-granular. Enforce precise boundaries locally too.
        if start:
            query += " since:" + start.date().isoformat()
        query += " until:" + (end.date() + timedelta(days=1)).isoformat()
        if params.get("since_id"):
            query += " since_id:" + params["since_id"]
        result = self.call(self.client.search_tweet, query=query, product="Latest",
                           count=min(20, params["max_results"]), cursor=params.get("next_token"))
        posts, users = [], {}
        raw_count = len(result)
        for tweet in result:
            created = tweet.created_at_datetime
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            created = created.astimezone(timezone.utc)
            if created > end or (start and created < start):
                continue
            if params.get("since_id") and int(tweet.id) <= int(params["since_id"]):
                continue
            user = getattr(tweet, "user", None)
            user_id = str(getattr(user, "id", ""))
            expected_authors = core.re.findall(r"\bfrom:([A-Za-z0-9_]+)", params["query"], flags=core.re.I)
            if expected_authors and getattr(user, "screen_name", "").casefold() not in {h.casefold() for h in expected_authors}:
                continue
            if user_id:
                users[user_id] = {"id": user_id, "username": getattr(user, "screen_name", "")}
            posts.append({"id": str(tweet.id), "author_id": user_id,
                          "created_at": core.stamp(created),
                          "text": getattr(tweet, "full_text", None) or tweet.text,
                          "conversation_id": str(getattr(tweet, "conversation_id", tweet.id)),
                          "entities": {"urls": [u for u in (getattr(tweet, "urls", None) or []) if isinstance(u, dict)]},
                          "source": "twikit"})
        token = getattr(result, "next_cursor", None) if raw_count else None
        return {"data": posts, "includes": {"users": list(users.values())},
                "meta": {"next_token": token} if token else {}, "_returned_count": raw_count}

    def close(self):
        if self.loop.is_closed():
            return
        http = getattr(self.client, "http", None)
        try:
            if http is not None and hasattr(http, "aclose"):
                self.loop.run_until_complete(http.aclose())
        finally:
            self.loop.close()
