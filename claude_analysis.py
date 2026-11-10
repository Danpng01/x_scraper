"""Anthropic Messages-compatible transport, including custom Claude gateways."""
import json
import urllib.request
import urllib.error
import urllib.parse
from config import load_config
import gemini_analysis as engine
import x_stock_ideas as core

DEFAULT_BASE_URL = "https://lum.id/claude"
DEFAULT_MODEL = "claude-sonnet-5-5"


def load_settings(path):
    values = load_config(path)
    token = values.get("ANTHROPIC_AUTH_TOKEN") or values.get("ANTHROPIC_API_KEY", "")
    if not token or token.startswith("PASTE_"):
        raise core.CollectorError("Set ANTHROPIC_AUTH_TOKEN in the local .env.")
    return token, values.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL, values.get("ANTHROPIC_MODEL") or values.get("LUMID_MODEL") or DEFAULT_MODEL


class ClaudeClient(engine.GeminiClient):
    def __init__(self, auth_token, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL,
                 max_requests=20, rpm=4, max_wait=900, opener=None, db=None):
        base = urllib.parse.urlsplit(base_url)
        if base.scheme != "https" or not base.netloc or base.username or base.password or base.query or base.fragment:
            raise core.CollectorError("ANTHROPIC_BASE_URL must be an HTTPS URL without embedded credentials, query or fragment.")
        if not model or not isinstance(model, str):
            raise core.CollectorError("Set ANTHROPIC_MODEL in .env.")
        if rpm <= 0:
            raise core.CollectorError("AI RPM must be positive.")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        super_opener = opener or urllib.request.build_opener(NoRedirect()).open
        core.XClient.__init__(self, auth_token, max_requests, max_wait, retries=1,
                              interval=60 / rpm, opener=super_opener)
        self.model, self.db = model, db
        base_url = base_url.rstrip("/")
        self.url = base_url + ("/messages" if base_url.endswith("/v1") else "/v1/messages")

    def generate(self, request_body):
        system = request_body["systemInstruction"]["parts"][0]["text"]
        system += "\nReturn exactly one JSON object without Markdown. JSON schema:\n" + json.dumps(engine.RESPONSE_SCHEMA, separators=(",", ":"))
        payload = {"model": self.model, "max_tokens": request_body["generationConfig"]["maxOutputTokens"],
                   "temperature": 0.1, "system": system,
                   "messages": [{"role": "user", "content": request_body["contents"][0]["parts"][0]["text"]}]}
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        request = urllib.request.Request(self.url, data=serialized.encode(), method="POST", headers={
            "Content-Type": "application/json", "x-api-key": self.token,
            "anthropic-version": "2023-06-01"})
        for attempt in range(self.retries + 1):
            if self.requests >= self.max_requests:
                raise core.PauseCollection("AI request cap reached. Completed results are cached; rerun --analyze-only.")
            self.wait(self.not_before - core.time.time(), "Claude pacing / reset")
            self.requests += 1
            self.not_before = core.time.time() + self.interval
            try:
                with self.opener(request, timeout=90) as response:
                    raw = response.read()
                try:
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    self.ledger("invalid-json", len(serialized))
                    raise core.CollectorError("Claude gateway returned invalid JSON; inputs remain queued.") from None
                usage = result.get("usage") or {}
                tokens_in = usage.get("input_tokens", 0)
                tokens_out = usage.get("output_tokens", 0)
                self.ledger("200", len(serialized), {"promptTokenCount": tokens_in,
                            "candidatesTokenCount": tokens_out, "totalTokenCount": tokens_in + tokens_out})
                if result.get("stop_reason") != "end_turn":
                    raise core.CollectorError("Claude output did not finish normally; inputs remain queued. Try a smaller --batch-size.")
                text = "".join(p.get("text", "") for p in result.get("content", []) if p.get("type") == "text")
                try:
                    return json.loads(text)
                except ValueError:
                    raise core.CollectorError("Claude output was not a JSON object; inputs remain queued.") from None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace").replace(self.token, "[REDACTED]")
                self.ledger(str(exc.code), len(serialized))
                if exc.code == 429:
                    if any(word in body.lower() for word in ("daily", "perday", "quota", "credit", "balance")):
                        raise core.PauseCollection("Claude gateway quota/balance limit reached; progress is cached. Check your gateway allowance.") from None
                    if attempt < self.retries:
                        self.not_before = max(self.not_before, core.time.time() + core.retry_delay(exc.headers, attempt))
                        continue
                    raise core.PauseCollection("Claude rate limit persists; rerun --analyze-only later.") from None
                if exc.code in {500, 502, 503, 504} and attempt < self.retries:
                    self.not_before = max(self.not_before, core.time.time() + 5)
                    continue
                raise core.CollectorError(f"Claude gateway HTTP {exc.code}: {body[:350]}. Check base URL, auth token and model.") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                self.ledger("network-error", len(serialized))
                raise core.PauseCollection("Claude network failure; inputs remain queued without automatic replay.") from None
