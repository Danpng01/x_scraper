"""Cached public US symbol reference and optional user-supplied company aliases."""

import csv
import io
import re
import urllib.request
from collections import defaultdict
from pathlib import Path

import x_stock_ideas as core


REFERENCE_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]
AMBIGUOUS_CODES = {"A", "AI", "ALL", "AN", "ARE", "ARM", "AT", "BE", "CAN", "FOR",
                   "GO", "HAS", "HE", "IT", "NOW", "ON", "OR", "SO", "TO", "YOU"}


def normalize_name(value):
    value = value.split(" - ", 1)[0]
    value = re.split(r"\b(?:common stock|ordinary shares|class [a-z] (?:common|ordinary))\b", value, flags=re.I)[0]
    words = re.findall(r"[a-z0-9]+", value.casefold())
    while words and words[-1] in {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc"}:
        words.pop()
    return " ".join(words)


def parse_nasdaq(text):
    result = []
    for row in csv.DictReader(io.StringIO(text), delimiter="|"):
        symbol = row.get("Symbol") or row.get("ACT Symbol") or ""
        name = row.get("Security Name", "")
        if (not core.TICKER.fullmatch(symbol) or row.get("Test Issue") != "N" or
                row.get("ETF") == "Y" or not name):
            continue
        if re.search(r"\b(warrants?|preferred stock|rights to)\b", name, flags=re.I):
            continue
        result.append({"ticker": symbol, "name": name, "aliases": ""})
    return result


class CompanyDirectory:
    def __init__(self, rows=()):
        self.symbols = {}
        self.names = defaultdict(set)
        for row in rows:
            symbol = (row.get("ticker") or "").strip().upper()
            if not core.TICKER.fullmatch(symbol):
                continue
            self.symbols[symbol] = (row.get("name") or "").strip()
            names = [row.get("name") or ""] + (row.get("aliases") or "").split(";")
            for name in names:
                normalized = normalize_name(name)
                if normalized:
                    self.names[normalized].add(symbol)

    @classmethod
    def load(cls, folder, custom=None, refresh=False, offline=False):
        path = Path(custom) if custom else Path(folder) / "company_directory.csv"
        if not custom and not offline and (refresh or not path.exists() or
                                            core.time.time() - path.stat().st_mtime > 86400):
            try:
                rows = []
                for url in REFERENCE_URLS:
                    request = urllib.request.Request(url, headers={"User-Agent": "stock-research/1.0"})
                    with urllib.request.urlopen(request, timeout=20) as response:
                        data = response.read(8_000_001)
                    if len(data) > 8_000_000:
                        raise core.CollectorError("Unexpectedly large symbol directory response.")
                    parsed = parse_nasdaq(data.decode("utf-8-sig"))
                    if not parsed:
                        raise core.CollectorError("Symbol directory format was not recognized.")
                    rows.extend(parsed)
                temporary = path.with_suffix(".tmp")
                with temporary.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["ticker", "name", "aliases"])
                    writer.writeheader()
                    writer.writerows(rows)
                temporary.replace(path)
                core.log(f"Updated cached US symbol reference: {len(rows)} records.")
            except (OSError, ValueError, core.CollectorError):
                core.log("Could not refresh the symbol reference; cached matches will be used if available. Unmatched tickers remain unverified.")
        if path.exists():
            with path.open(encoding="utf-8-sig", newline="") as handle:
                return cls(csv.DictReader(handle))
        return cls()

    def resolve(self, item, source_text):
        """Validate listing/name matches, while retaining unsupported mappings as unresolved."""
        name, mention = item["name"], item["mention"]
        candidate = item["ticker"].upper().removeprefix("$")
        if candidate and not core.TICKER.fullmatch(candidate):
            candidate = ""
        direct_names = self.names.get(normalize_name(mention), set())
        canonical_names = self.names.get(normalize_name(name), set())
        explicit = bool(candidate and re.search(r"(?<!\w)\$?" + re.escape(candidate) + r"(?!\w)", source_text))
        cashtag = bool(candidate and re.search(r"\$" + re.escape(candidate) + r"(?!\w)", source_text, re.I))
        context = re.search(r"\b(shares?|stock|earnings|nasdaq|nyse|ticker)\b", item["evidence"], re.I)
        ambiguous = candidate in AMBIGUOUS_CODES and not cashtag and not context and not direct_names
        ticker, status = "", "unresolved"
        if len(direct_names) == 1:
            ticker, status = next(iter(direct_names)), "reference_name"
        elif len(direct_names) > 1:
            if explicit and candidate in direct_names:
                ticker, status = candidate, "reference_symbol"
            else:
                status = "ambiguous_share_class"
        elif candidate in self.symbols and explicit and not ambiguous:
            ticker, status = candidate, "reference_symbol"
        elif len(canonical_names) == 1 and not ambiguous:
            proposed = next(iter(canonical_names))
            if not candidate or candidate == proposed:
                ticker, status = proposed, "model_alias_reference_symbol"
        elif candidate and explicit and not ambiguous:
            ticker, status = candidate, "unverified_explicit_symbol"
        elif ambiguous:
            status = "ambiguous_symbol"
        return {**item, "ticker": ticker, "candidate_ticker": candidate,
                "mapping_status": status,
                "reference_name": self.symbols.get(ticker, "")}
