#!/usr/bin/env python3
"""
Ledger — a company governance and corporate-issues explorer.

Search any listed company and see:
  * its leadership team, with factual, team-level summaries
  * every material disclosure our SEC filing scanner finds (linked to the filing)
  * a curated, cited list of known corporate problems

Data sources are public and need no API key:
  * Yahoo Finance  — quote, profile and the officers list
  * SEC EDGAR      — ticker→CIK map, full-text filing search, filings index

Run:  python server.py
Open: http://127.0.0.1:8770
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hmac
import http.cookiejar
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
CACHE_DIR = APP_DIR / "cache"
CURATED_PATH = DATA_DIR / "curated_issues.json"
VERSION = "1.0.0"

SEARCH_TTL = 30 * 60
COMPANY_TTL = 30 * 60
CIK_TTL = 24 * 60 * 60
FILINGS_TTL = 6 * 60 * 60
DISK_TTL = 14 * 24 * 60 * 60

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# SEC asks for a descriptive agent with contact details.
UA_SEC = os.environ.get(
    "LEDGER_SEC_UA", "Ledger Governance Explorer contact@example.com"
)
SYMBOL_RE = re.compile(r"^[A-Z0-9.\-=^]{1,20}$")

# Disclosure themes scanned in SEC filings. Each keyword maps to a category the
# UI groups results under. Phrases are quoted so the full-text engine matches
# them exactly.
DISCLOSURE_KEYWORDS = [
    ("material weakness", "Accounting & controls", "high"),
    ("restatement", "Accounting & controls", "high"),
    ("non-reliance", "Accounting & controls", "high"),
    ("internal control over financial reporting", "Accounting & controls", "medium"),
    ("SEC investigation", "Regulatory & legal", "high"),
    ("subpoena", "Regulatory & legal", "high"),
    ("Department of Justice", "Regulatory & legal", "high"),
    ("consent order", "Regulatory & legal", "medium"),
    ("class action", "Litigation", "high"),
    ("settlement agreement", "Litigation", "medium"),
    ("judgment against", "Litigation", "medium"),
    ("going concern", "Financial distress", "high"),
    ("covenant waiver", "Financial distress", "high"),
    ("default under", "Financial distress", "high"),
    ("impairment charge", "Operations", "medium"),
    ("data breach", "Cybersecurity", "high"),
    ("cybersecurity incident", "Cybersecurity", "high"),
    ("product recall", "Operations", "medium"),
    ("related party transaction", "Governance", "medium"),
    ("executive departed", "Governance", "medium"),
    ("downgrade", "Credit", "low"),
    ("activist investor", "Governance", "low"),
    ("workforce reduction", "Operations", "low"),
]

POPULAR = [
    ("AAPL", "Apple Inc.", "Technology"),
    ("MSFT", "Microsoft Corporation", "Technology"),
    ("NVDA", "NVIDIA Corporation", "Technology"),
    ("AMZN", "Amazon.com, Inc.", "Consumer Cyclical"),
    ("GOOGL", "Alphabet Inc.", "Communication Services"),
    ("META", "Meta Platforms, Inc.", "Communication Services"),
    ("TSLA", "Tesla, Inc.", "Consumer Cyclical"),
    ("JPM", "JPMorgan Chase & Co.", "Financial Services"),
    ("KO", "The Coca-Cola Company", "Consumer Defensive"),
    ("JNJ", "Johnson & Johnson", "Healthcare"),
    ("XOM", "Exxon Mobil Corporation", "Energy"),
    ("BA", "The Boeing Company", "Industrials"),
    ("T", "AT&T Inc.", "Communication Services"),
    ("WFC", "Wells Fargo & Company", "Financial Services"),
    ("META", "Meta Platforms, Inc.", "Communication Services"),
    ("NKE", "NIKE, Inc.", "Consumer Cyclical"),
    ("DIS", "The Walt Disney Company", "Communication Services"),
    ("PSKY", "Paramount Skydance Corporation", "Communication Services"),
    ("HSBC", "HSBC Holdings plc", "Financial Services"),
    ("BABA", "Alibaba Group Holding Limited", "Consumer Cyclical"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_float(value, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        raw = value.get("raw")
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class FetchError(RuntimeError):
    pass


class Yahoo:
    """Quote + profile + officers from Yahoo, with a cookie/crumb session."""

    def __init__(self):
        self._lock = threading.Lock()
        self._opener = None
        self._crumb = None
        self._born = 0.0
        self._search_cache = {}

    def _session(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.addheaders = [
            ("User-Agent", UA_BROWSER),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]
        with opener.open("https://finance.yahoo.com/quote/AAPL/", timeout=18) as r:
            r.read(64_000)
        crumb = opener.open(
            "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=12
        ).read().decode("utf-8", "replace").strip()
        if not crumb or len(crumb) > 40:
            raise FetchError("Yahoo returned an invalid session")
        self._opener = opener
        self._crumb = crumb
        self._born = time.time()

    def _reset(self):
        self._opener = None
        self._crumb = None
        self._born = 0.0

    def _json(self, url, timeout=22, headers=None):
        opener = self._opener
        if opener is None:
            opener = urllib.request.build_opener()
            opener.addheaders = [("User-Agent", UA_BROWSER)]
        req = urllib.request.Request(url)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        with opener.open(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def search(self, query: str):
        key = query.strip().lower()
        cached = self._search_cache.get(key)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
        params = urllib.parse.urlencode(
            {
                "q": query,
                "quotesCount": 16,
                "newsCount": 0,
                "quotesQueryId": "tss_match_phrase_query",
            }
        )
        data = self._json(f"https://query1.finance.yahoo.com/v1/finance/search?{params}")
        out = []
        for q in data.get("quotes", []) or []:
            if q.get("quoteType") not in ("EQUITY", "ADR"):
                continue
            out.append(
                {
                    "symbol": q.get("symbol"),
                    "name": q.get("longname") or q.get("shortname") or q.get("symbol"),
                    "exchange": q.get("exchDisp") or q.get("exchange") or "",
                    "type": q.get("typeDisp") or "Equity",
                }
            )
        self._search_cache[key] = (time.time(), out)
        return out

    def profile(self, symbol: str):
        symbol = symbol.upper()
        last_error = None
        for _ in range(2):
            with self._lock:
                try:
                    if self._opener is None or time.time() - self._born > 60 * 60:
                        self._reset()
                        self._session()
                    encoded = urllib.parse.quote(symbol, safe="")
                    crumb = urllib.parse.quote(self._crumb or "", safe="")
                    url = (
                        "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
                        f"{encoded}?modules=assetProfile,price&crumb={crumb}"
                    )
                    data = self._json(url)
                    result = (data.get("quoteSummary", {}).get("result") or [None])[0]
                    if not result:
                        raise FetchError("Empty profile response")
                    return result
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, FetchError) as exc:
                    last_error = exc
                    self._reset()
            time.sleep(1.0)
        raise FetchError(f"Could not load profile for {symbol}: {last_error}")


class Sec:
    """SEC EDGAR: tickers, full-text filing search, and filing links."""

    def __init__(self):
        self._cik_lock = threading.Lock()
        self._cik_map = None
        self._cik_at = 0.0

    def _get(self, url, timeout=30, accept="application/json"):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA_SEC, "Accept": accept}
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()

    def cik_map(self):
        with self._cik_lock:
            if self._cik_map and time.time() - self._cik_at < CIK_TTL:
                return self._cik_map
            try:
                raw = self._get("https://www.sec.gov/files/company_tickers.json")
                data = json.loads(raw)
                mapping = {}
                for row in data.values():
                    ticker = str(row.get("ticker", "")).upper()
                    if ticker:
                        mapping[ticker] = {
                            "cik": str(row.get("cik_str", "")).zfill(10),
                            "title": row.get("title", ""),
                        }
                self._cik_map = mapping
                self._cik_at = time.time()
            except Exception as exc:
                print(f"  [sec] ticker map failed: {exc}", file=sys.stderr)
                self._cik_map = self._cik_map or {}
            return self._cik_map

    def filings_for(self, cik: str, keywords, lookback_years=3, per_keyword=3):
        """Full-text search filings for this company against each keyword."""
        if not cik:
            return []
        start = datetime.now(timezone.utc).date()
        start = start.replace(year=start.year - lookback_years).isoformat()
        results = []

        def search(item):
            phrase, category, severity = item
            # The SEC's full-text API returns nothing when several combined
            # form filters are present, so we query the broad core forms and
            # keep amendments (10-K/A, 8-K/A …) via a root-form check below.
            params = urllib.parse.urlencode(
                {
                    "q": f'"{phrase}"',
                    "ciks": cik,
                    "forms": "8-K,10-K,10-Q,20-F,6-K",
                    "dateRange": "custom",
                    "startdt": start,
                    "enddt": datetime.now(timezone.utc).date().isoformat(),
                }
            )
            try:
                data = json.loads(
                    self._get("https://efts.sec.gov/LATEST/search-index?" + params)
                )
            except Exception:
                return []
            hits = (data.get("hits") or {}).get("hits") or []
            out = []
            for hit in hits[:per_keyword]:
                src = hit.get("_source") or {}
                adsh = src.get("adsh")
                doc = str(hit.get("_id", "")).split(":", 1)[-1]
                if not adsh:
                    continue
                cik_int = str(int(cik))
                archive = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{adsh.replace('-', '')}"
                out.append(
                    {
                        "keyword": phrase,
                        "category": category,
                        "severity": severity,
                        "form": src.get("form") or (src.get("root_forms") or [""])[0],
                        "filed": src.get("file_date"),
                        "period": src.get("period_ending"),
                        "filingUrl": f"{archive}/{doc}" if doc else f"{archive}/",
                        "indexUrl": f"{archive}/",
                        "excerpt": src.get("file_description") or "",
                    }
                )
            return out

        workers = min(6, len(keywords))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(search, item): item for item in keywords}
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except Exception:
                    continue
        return results

    # A keyword hit is only a real signal if the filing actually contains the
    # term. The full-text index also matches boilerplate (audit opinions that
    # merely *refer to* a material weakness, M&A clauses mentioning appeals and
    # subpoenas), so every candidate document is downloaded and verified.
    SIGNAL_PATTERNS = {
        "material weakness": r"material weakness",
        "restatement": r"\brestatement",
        "non-reliance": r"non-reliance",        "internal control over financial reporting": r"internal control over financial reporting",
        "SEC investigation": r"(securities and exchange commission|the sec)\b[\s\S]{0,60}?\b(investigation|inquiry|investigating)",
        "subpoena": r"\bsubpoena",
        "Department of Justice": r"department of justice",
        "consent order": r"consent order",
        "class action": r"class action",
        "settlement agreement": r"settlement agreement",
        "judgment against": r"judgment against",
        "going concern": r"going concern",
        "covenant waiver": r"covenant waiver",
        "default under": r"default under",
        "impairment charge": r"impairment (charge|of|loss)",
        "data breach": r"data breach",
        "cybersecurity incident": r"cybersecurity incident",
        "product recall": r"product recall",
        "related party transaction": r"related party transaction",
        "executive departed": r"executive departed",
        "downgrade": r"downgrade",
        "activist investor": r"activist investor",
        "workforce reduction": r"workforce reduction",
    }

    @staticmethod
    def _tidy_excerpt(excerpt: str) -> str:
        """Remove replacement characters and stray punctuation at the edges."""
        text = (excerpt or "").replace("\ufffd", " ").replace("&#146;", "'")
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^[^A-Z0-9\"'(]+", "", text)
        text = re.sub(r"\s+([,.;:])", r"\1", text)
        return text.strip()

    def verify_document(self, url: str, keyword: str, pattern: str = None):
        """Download a filing and confirm it contains the keyword.

        Returns a short excerpt around the first match, or None.
        """
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA_SEC, "Accept": "*/*"}
            )
            with urllib.request.urlopen(req, timeout=45) as response:
                raw = response.read(6_000_000)
        except Exception:
            return None
        text = raw.decode("utf-8", "ignore")
        # Strip tags BEFORE decoding entities. XBRL text carries encoded
        # style attributes like style=\"font-family:...\"; decoding first would
        # make those look like normal words and leak them into excerpts.
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&#?\w+;", " ", text)
        text = re.sub(r"\.{3,}", " ", text)
        text = text.replace("\ufffd", " ")
        text = re.sub(r"\s+", " ", text)
        regex = pattern or self.SIGNAL_PATTERNS.get(keyword) or re.escape(keyword)
        match = re.search(regex, text, re.IGNORECASE)
        if not match:
            return None
        start = max(0, match.start() - 170)
        end = min(len(text), match.end() + 230)
        excerpt = text[start:end].strip()
        if start > 0:
            excerpt = "…" + excerpt
        if end < len(text):
            excerpt = excerpt + "…"
        return excerpt
    def scan(self, cik: str, keywords, lookback_years=3, max_results=40):
        """Find, verify and rank material disclosures for one company."""
        candidates = self.filings_for(cik, keywords, lookback_years=lookback_years)
        if not candidates:
            return [], 0
        # One verification per unique document keeps the request count sane.
        unique_docs = {}
        for item in candidates:
            unique_docs.setdefault(item["filingUrl"], []).append(item)

        verified = []

        def check(url):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": UA_SEC, "Accept": "*/*"}
                )
                with urllib.request.urlopen(req, timeout=45) as response:
                    raw = response.read(6_000_000)
            except Exception:
                return url, None
            text = raw.decode("utf-8", "ignore")
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"&#?\w+;", " ", text)
            text = re.sub(r"\.{3,}", " ", text)
            text = text.replace("\ufffd", " ")
            text = re.sub(r"\s+", " ", text)
            return url, text

        max_docs = 24
        urls = list(unique_docs.keys())[:max_docs]
        documents = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            for url, text in pool.map(check, urls):
                if text:
                    documents[url] = text

        for url, text in documents.items():
            for item in unique_docs[url]:
                regex = self.SIGNAL_PATTERNS.get(item["keyword"])
                match = re.search(regex, text, re.IGNORECASE) if regex else None
                if not match:
                    continue
                start = max(0, match.start() - 170)
                end = min(len(text), match.end() + 230)
                excerpt = text[start:end].strip()
                if start > 0:
                    excerpt = "…" + excerpt
                if end < len(text):
                    excerpt = excerpt + "…"
                if self.is_boilerplate(excerpt):
                    continue
                verified.append({**item, "verified": True, "excerpt": self._tidy_excerpt(excerpt)})

        verified.sort(key=lambda i: (i.get("filed") or "", i.get("severity") or ""), reverse=True)
        return verified[:max_results], len(candidates)

    BOILERPLATE_MARKERS = (
        "our audit included obtaining an understanding",
        "reporting was maintained in all material respects",
        "clawback",
        "covered executive",
        "certifying officer",
        "exchange act rules 13a-15",
        "unless otherwise provided",
        "from time to time",
        "on the securities exchange",
        "party&#146;s",
        "the party's",
        "material respects",
        "included in the audit",
        "auditor who expressed",
        "is not an audit",
    )

    @classmethod
    def is_boilerplate(cls, excerpt: str) -> bool:
        """Reject excerpts that are standard risk-factor or audit language."""
        low = (excerpt or "").lower()
        if any(marker in low for marker in cls.BOILERPLATE_MARKERS):
            return True
        # XBRL and styling remnants leak through tag stripping.
        if low.count("font-") > 1 or "\ufffd" in low:
            return True
        # Tables of contents (long runs of dotted leaders) and plan documents
        # mention "restatement" and "non-reliance" without describing an issue.
        if low.count(".") > 12 or low.count(".....") > 0:
            return True
        if re.search(r"(restated|restatement) (plan|program|policy|agreement|deferred compensation|serp)", low):
            return True
        if re.search(r"non-reliance (on|of) (any|another|third)", low):
            return True
        # Hard-coded merge clauses that mention "restatement" or "subpoena".
        if re.search(r"merger agreement|purchase agreement|this agreement", low):
            return True
        if re.search(r"(shall|may|will) be (deemed|construed)", low):
            return True
        # Standard bond indentures describe a future "event of default".
        if re.search(r"will not constitute an event of default|shall not constitute an event of default|is not an event of default", low):
            return True
        # Generic insider-trading risk-factor language.
        if re.search(r"insider trading (can|may) include|securities and exchange commission \(the sec\)", low):
            return True
        # "Restatement of prior period amounts" is usually cosmetic reclass.
        if re.search(r"restatem\w* of (prior|previously reported)", low) and "material" not in low:
            return True
        return False


YAHOO = Yahoo()
SEC = Sec()


class Service:
    def __init__(self):
        self._lock = threading.Lock()
        self._company_cache = {}
        self._filings_cache = {}
        self._curated = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ---------- helpers ----------
    @staticmethod
    def _disk_path(symbol):
        return CACHE_DIR / f"{symbol.replace('/', '_')}.json"

    def _disk_read(self, symbol):
        path = self._disk_path(symbol)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data.get("_cachedAt", 0) <= DISK_TTL:
                return data
        except Exception:
            return None
        return None

    def _disk_write(self, symbol, payload):
        try:
            self._disk_path(symbol).write_text(
                json.dumps({**payload, "_cachedAt": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def curated(self):
        with self._lock:
            if self._curated is not None:
                return self._curated
            try:
                self._curated = json.loads(CURATED_PATH.read_text(encoding="utf-8"))
            except Exception:
                self._curated = {}
            return self._curated

    def curated_issues(self, symbol, company_name):
        data = self.curated()
        entry = (data.get("companies") or {}).get(symbol.upper())
        if entry:
            return {
                "matched": True,
                "matchType": "ticker",
                "note": entry.get("note") or "",
                "issues": entry.get("issues") or [],
                "reviewedAt": entry.get("reviewedAt"),
            }
        # Fall back to a curated company matched by name, for renamed tickers.
        name_key = (company_name or "").strip().lower()
        for ticker, entry in (data.get("companies") or {}).items():
            if entry.get("name", "").strip().lower() == name_key:
                return {
                    "matched": True,
                    "matchType": "name",
                    "note": entry.get("note") or "",
                    "issues": entry.get("issues") or [],
                    "reviewedAt": entry.get("reviewedAt"),
                    "ticker": ticker,
                }
        return {
            "matched": False,
            "matchType": "none",
            "note": data.get("coverageNote") or "",
            "issues": [],
            "reviewedAt": None,
        }

    # ---------- team summariser ----------
    @staticmethod
    def normalise_officer(officer):
        name = (officer.get("name") or "").strip()
        title = (officer.get("title") or "").strip()
        age = safe_float(officer.get("age"))
        born = safe_float(officer.get("yearBorn"))
        pay = safe_float((officer.get("totalPay") or {}).get("raw"))
        exercised = safe_float((officer.get("exercisedValue") or {}).get("raw"))
        unexercised = safe_float((officer.get("unexercisedValue") or {}).get("raw"))
        if not age and born:
            try:
                age = datetime.now(timezone.utc).year - int(born)
            except Exception:
                age = None
        clean_name = re.sub(r"^(Mr\.|Ms\.|Mrs\.|Dr\.)\s*", "", name)
        clean_name = re.sub(r"\s+", " ", clean_name).strip()
        return {
            "name": clean_name,
            "rawName": name,
            "title": title,
            "age": age,
            "pay": pay,
            "exercisedValue": exercised,
            "unexercisedValue": unexercised,
            "fiscalYear": officer.get("fiscalYear"),
            "isChief": bool(re.search(r"\bCEO\b|\bChief Executive\b", title, re.I)),
            "isChair": bool(re.search(r"\bChair", title, re.I)),
            "isCFO": bool(re.search(r"\bCFO\b|\bChief Financial\b", title, re.I)),
        }

    # ---------- fact-based executive scores ----------
    #
    # This is a checklist of things that appear in the public record, NOT a
    # judgement of competence. Each factor is shown with its value and points
    # so a reader can disagree with any single input. Missing data pays no
    # penalty and no bonus: the maximum simply shrinks (see `scoreBy`).
    FACTOR_MAX = 100

    @staticmethod
    def _band_points(value, bands):
        """bands = [(threshold, points), ...] evaluated high-to-low."""
        if value is None:
            return None
        for threshold, points in bands:
            if value >= threshold:
                return points
        return bands[-1][1] if bands else 0

    @classmethod
    def score_officer(cls, officer, team_context):
        factors = []
        total = 0
        possible = 0

        age = officer.get("age")
        age_points = None
        if age is not None:
            if age < 55:
                age_points = 20
            elif age < 60:
                age_points = 16
            elif age < 65:
                age_points = 11
            elif age < 70:
                age_points = 6
            else:
                age_points = 2
        factors.append(
            {
                "label": "Age / succession risk",
                "value": f"{int(age)}" if age is not None else "not disclosed",
                "points": age_points,
                "max": 20,
                "why": "Younger executives carry less near-term succession risk.",
            }
        )

        # Pay spread inside the team. A large gap is common at the top of a
        # company, so this is scored gently rather than treated as a fault.
        pay = officer.get("pay")
        median_pay = team_context.get("medianPay")
        pay_ratio = team_context.get("payRatio", {}).get(officer["name"])
        pay_points = None
        if pay is not None and median_pay:
            if pay_ratio <= 2.5:
                pay_points = 20
            elif pay_ratio <= 4:
                pay_points = 15
            elif pay_ratio <= 7:
                pay_points = 9
            else:
                pay_points = 3
        factors.append(
            {
                "label": "Pay relative to team",
                "value": f"{pay_ratio:.1f}× team median" if pay_ratio is not None else "not disclosed",
                "points": pay_points,
                "max": 20,
                "why": "How disclosed pay compares with the median of this leadership team.",
            }
        )

        # Role concentration: a CEO who also chairs concentrates power.
        conflict_points = 20
        conflict_note = "No combined CEO/chair role."
        if officer.get("isChief") and officer.get("isChair"):
            conflict_points = 4
            conflict_note = "CEO and chair are the same person."
        elif officer.get("isChief") or officer.get("isChair"):
            conflict_points = 14
            conflict_note = "Holds the CEO or chair role."
        factors.append(
            {
                "label": "Separation of duties",
                "value": conflict_note,
                "points": conflict_points,
                "max": 20,
                "why": "Independent chairing is a standard governance safeguard.",
            }
        )

        # Equity ownership: some skin in the game is a positive, but a very
        # large multiple means the executive is mostly paid in stock, which
        # cuts both ways. "No equity reported" is common for executives who do
        # not appear individually in the proxy, so it is treated as neutral.
        option_value = (officer.get("exercisedValue") or 0) + (officer.get("unexercisedValue") or 0)
        equity_points = None
        equity_value = "not disclosed"
        if pay is not None and pay > 0 and option_value > 0:
            ratio = option_value / pay
            equity_value = f"{ratio:.1f}× annual pay"
            if ratio <= 2:
                equity_points = 10
            elif ratio <= 10:
                equity_points = 15
            elif ratio <= 30:
                equity_points = 11
            else:
                equity_points = 5
        factors.append(
            {
                "label": "Equity held",
                "value": equity_value,
                "points": equity_points,
                "max": 20,
                "why": "Share ownership ties payouts to long-term shareholders. Neutral when not disclosed.",
            }
        )

        for factor in factors:
            if factor["points"] is not None:
                total += factor["points"]
                possible += factor["max"]

        score = round(total / possible * 100) if possible else None
        # A score built from one or two factors is not a rating. Require at
        # least half the checklist (50 of 100 points) to publish a number.
        if score is not None and possible < 50:
            score = None
        # Half the checklist should also not read as a perfect record.
        if score is not None:
            score = min(score, 92)
        grade = "Not enough data"
        if score is not None:
            if score >= 80:
                grade = "Strong on the record"
            elif score >= 65:
                grade = "Sound on the record"
            elif score >= 50:
                grade = "Mixed"
            else:
                grade = "Weak signals"

        return {
            "score": score,
            "grade": grade,
            "basis": f"{possible} of {cls.FACTOR_MAX} possible points were measurable.",
            "factors": factors,
        }

    @classmethod
    def score_team(cls, officers):
        team = [o for o in officers if o["name"]]
        pays = sorted(o["pay"] for o in team if o.get("pay"))
        median_pay = pays[len(pays) // 2] if pays else None
        context = {"payRatio": {}, "medianPay": median_pay}
        for officer in team:
            pay = officer.get("pay")
            if pay and median_pay:
                context["payRatio"][officer["name"]] = pay / median_pay
        scored = []
        for officer in team:
            result = cls.score_officer(officer, context)
            scored.append({**officer, "rating": result})
        numeric = [o["rating"]["score"] for o in scored if o["rating"]["score"] is not None]
        team_score = round(sum(numeric) / len(numeric)) if numeric else None
        return {
            "officers": scored,
            "teamScore": team_score,
            "measured": len(numeric),
            "total": len(team),
            "medianPay": median_pay,
        }

    @staticmethod
    def team_summary(officers, employees=None, pay_ratio=None):
        team = [o for o in officers if o["name"]]
        ages = [o["age"] for o in team if o["age"]]
        pays = [o["pay"] for o in team if o["pay"]]
        chiefs = [o for o in team if o["isChief"]]
        chairs = [o for o in team if o["isChair"]]
        ceo = chiefs[0] if chiefs else None
        ceo_chairs = [o for o in team if o["isChief"] and o["isChair"]]
        median_age = sorted(ages)[len(ages) // 2] if ages else None
        oldest = max(team, key=lambda o: (o["age"] or 0)) if ages else None
        newest_pay = max(pays) if pays else None
        total_pay = sum(pays) if pays else None
        ceo_pay = ceo["pay"] if ceo and ceo["pay"] else None
        ceo_share = (ceo_pay / total_pay) if (ceo_pay and total_pay) else None

        headcount = safe_float(employees)
        ceo_to_median_employee = None
        if ceo_pay and headcount and headcount > 0 and pay_ratio:
            # pay_ratio (if supplied) is median pay in currency; otherwise skip.
            pass

        facts = []
        if team:
            facts.append(f"{len(team)} executives disclosed in the company profile.")
        if ceo_chairs:
            facts.append(f"{len(ceo_chairs)} person holds both CEO and chair roles.")
        elif chairs:
            facts.append(f"The chair role is held separately from the CEO.")
        if median_age:
            facts.append(f"Median disclosed age is {median_age}.")
        if oldest and oldest["age"]:
            facts.append(f"The oldest listed executive is {oldest['name']} ({int(oldest['age'])}).")
        if ceo_pay:
            facts.append(f"The CEO's disclosed compensation is {round(ceo_pay):,} (fiscal {ceo['fiscalYear'] or 'n/a'}).")
        if ceo_share:
            facts.append(f"The CEO accounts for {round(ceo_share * 100)}% of disclosed executive pay on this list.")
        if headcount:
            facts.append(f"The company reports {int(headcount):,} employees.")
        if pays and not ceo_pay:
            facts.append("No CEO pay figure was disclosed in the profile — often the case for founders.")
        if not team:
            facts.append("No executive roster is published for this listing.")

        return {
            "count": len(team),
            "medianAge": median_age,
            "oldest": oldest["name"] if oldest else None,
            "ceoPay": ceo_pay,
            "totalPay": total_pay,
            "ceoShare": ceo_share,
            "separateChair": bool(chairs and not ceo_chairs),
            "combinedChair": bool(ceo_chairs),
            "facts": facts,
        }

    # ---------- public ----------
    def search(self, query):
        query = query.strip()
        if not query:
            return []
        try:
            return YAHOO.search(query)
        except Exception:
            needle = query.lower().replace(" ", "")
            return [
                {"symbol": s, "name": n, "exchange": "—", "type": "Equity"}
                for s, n, _ in POPULAR
                if needle in s.lower() or needle in n.lower().replace(" ", "")
            ][:10]

    def company(self, symbol):
        symbol = symbol.upper()
        if not SYMBOL_RE.match(symbol):
            raise ValueError("Invalid ticker symbol")
        now = time.time()
        with self._lock:
            cached = self._company_cache.get(symbol)
            if cached and now - cached[0] < COMPANY_TTL:
                return cached[1]

        raw = YAHOO.profile(symbol)
        profile = raw.get("assetProfile") or {}
        price = raw.get("price") or {}
        officers = [
            self.normalise_officer(o) for o in (profile.get("companyOfficers") or [])
        ]
        summary = self.team_summary(
            officers, employees=profile.get("fullTimeEmployees")
        )
        rated = self.score_team(officers)
        summary["teamScore"] = rated["teamScore"]
        summary["measured"] = rated["measured"]
        summary["officers"] = rated["officers"]
        cik_info = SEC.cik_map().get(symbol, {})

        payload = {
            "symbol": price.get("symbol") or symbol,
            "name": price.get("longName")
            or price.get("shortName")
            or cik_info.get("title")
            or symbol,
            "exchange": price.get("exchangeName") or "",
            "currency": price.get("currency") or "USD",
            "quoteType": price.get("quoteType") or "EQUITY",
            "price": safe_float(price.get("regularMarketPrice")),
            "sector": profile.get("sector") or "",
            "industry": profile.get("industry") or "",
            "country": profile.get("country") or "",
            "state": profile.get("state") or "",
            "city": profile.get("city") or "",
            "website": profile.get("website") or "",
            "employees": safe_float(profile.get("fullTimeEmployees")),
            "founded": None,
            "description": profile.get("longBusinessSummary") or "",
            "officers": rated["officers"],
            "team": summary,
            "cik": cik_info.get("cik"),
            "cikTitle": cik_info.get("title"),
            "meta": {"provider": "Yahoo Finance · SEC EDGAR", "fetchedAt": utc_now()},
        }
        with self._lock:
            self._company_cache[symbol] = (now, payload)
            self._disk_write(symbol, payload)
        return payload

    def disclosures(self, symbol):
        symbol = symbol.upper()
        now = time.time()
        with self._lock:
            cached = self._filings_cache.get(symbol)
            if cached and now - cached[0] < FILINGS_TTL:
                return cached[1]
        company = self.company(symbol)
        cik = company.get("cik")
        if not cik:
            result = {
                "symbol": symbol,
                "cik": None,
                "available": False,
                "reason": "No SEC CIK mapping was found for this listing.",
                "items": [],
                "checkedAt": utc_now(),
            }
        else:
            items, candidates = SEC.scan(cik, DISCLOSURE_KEYWORDS, lookback_years=3)
            result = {
                "symbol": symbol,
                "cik": cik,
                "available": True,
                "items": items,
                "keywords": len(DISCLOSURE_KEYWORDS),
                "lookbackYears": 3,
                "rawHits": candidates,
                "verifiedCount": len(items),
                "checkedAt": utc_now(),
            }
        with self._lock:
            self._filings_cache[symbol] = (now, result)
        return result

    def issues(self, symbol):
        company = self.company(symbol)
        return self.curated_issues(symbol, company.get("name"))

    def bundle(self, symbol):
        """Company + disclosures + curated issues in one response."""
        company = self.company(symbol)
        disclosures = self.disclosures(symbol)
        curated = self.curated_issues(symbol, company.get("name"))
        return {
            **company,
            "disclosures": disclosures,
            "curated": curated,
            "meta": {
                **company.get("meta", {}),
                "checkedAt": disclosures.get("checkedAt"),
            },
        }


SERVICE = Service()


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
# Ledger holds no API keys, tokens or personal data: it reads public company
# profiles and SEC filings and stores nothing about visitors. Publishing it
# raises three real risks instead, handled below:
#
#   * the URL driven as an unpaid proxy against upstream data,
#   * internals leaking through error text or the version header,
#   * the page framed or script-injected by another site.

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}

# Optional shared password; off unless LEDGER_PASSWORD is set.
AUTH_PASSWORD = os.environ.get("LEDGER_PASSWORD", "").strip()
AUTH_USER = os.environ.get("LEDGER_USERNAME", "ledger").strip() or "ledger"


class RateLimiter:
    """Fixed-window limiter so a public link cannot be scripted as a proxy."""

    def __init__(self, window=60):
        try:
            self.limit = int(os.environ.get("LEDGER_RATE_LIMIT", 120))
        except ValueError:
            self.limit = 120
        self.limit = max(10, min(self.limit, 5000))
        self.window = window
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None or now - bucket[0] >= self.window:
                self._hits[key] = [now, 1]
                if len(self._hits) > 1024:
                    for stale in [k for k, v in self._hits.items() if now - v[0] >= self.window]:
                        self._hits.pop(stale, None)
                return True, self.limit - 1
            if bucket[1] >= self.limit:
                return False, max(1, int(self.window - (now - bucket[0])))
            bucket[1] += 1
            return True, self.limit - bucket[1]


LIMITER = RateLimiter()


def _constant_time_compare(a, b):
    try:
        return hmac.compare_digest(str(a), str(b))
    except Exception:
        return False


def log_internal_error(context, exc):
    """Log full detail server-side; give the visitor only a short reference."""
    ref = uuid.uuid4().hex[:8]
    print(f"  [{context}] ref={ref} {type(exc).__name__}: {exc}", file=sys.stderr)
    return ref


class Handler(SimpleHTTPRequestHandler):
    # Do not advertise the version: it tells an attacker which bugs to try.
    server_version = "Ledger"
    sys_version = ""

    def version_string(self):
        return self.server_version

    def end_headers(self):
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def log_message(self, fmt, *args):
        if os.environ.get("LEDGER_QUIET") == "1":
            return
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    # ---------- access control ----------
    def _client_key(self):
        forwarded = self.headers.get("X-Forwarded-For") or ""
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        return self.client_address[0] if self.client_address else "unknown"

    def _authorised(self):
        if not AUTH_PASSWORD:
            return True
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
        except Exception:
            return False
        username, _, password = decoded.partition(":")
        return username == AUTH_USER and _constant_time_compare(password, AUTH_PASSWORD)

    def _require_auth(self):
        body = b"Authentication required."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Ledger", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _rate_limited(self):
        allowed, remaining = LIMITER.allow(self._client_key())
        if allowed:
            return False
        body = json.dumps(
            {"error": "Too many requests. Wait a moment and try again.",
             "retryAfterSeconds": remaining}
        ).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(remaining))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        return True

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _serve_static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(403, "Forbidden")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.exists() or not target.is_file():
            if "." not in Path(rel).name:
                target = STATIC_DIR / "index.html"
            else:
                self.send_error(404, "Not found")
                return
        mime, _ = mimetypes.guess_type(str(target))
        content = target.read_bytes()
        encoding = None
        if "gzip" in (self.headers.get("Accept-Encoding") or "").lower() and len(content) > 1024:
            if (mime or "").startswith("text/") or mime in (
                "application/javascript",
                "application/json",
                "image/svg+xml",
            ):
                content = gzip.compress(content, 6)
                encoding = "gzip"
        self.send_response(200)
        self.send_header(
            "Content-Type",
            (mime or "application/octet-stream")
            + ("; charset=utf-8" if (mime or "").startswith("text/") else ""),
        )
        self.send_header("Content-Length", str(len(content)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        if not self._authorised():
            self._require_auth()
            return
        if self._rate_limited():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/health":
            self._json(
                {
                    "ok": True,
                    "version": VERSION,
                    "time": utc_now(),
                    "curatedCompanies": len(
                        (SERVICE.curated().get("companies") or {})
                    ),
                }
            )
            return

        if path == "/api/popular":
            self._json({"results": [{"symbol": s, "name": n} for s, n, _ in POPULAR[:12]]})
            return

        if path == "/api/search":
            q = (query.get("q") or [""])[0]
            if not q.strip():
                self._json({"results": []})
                return
            if len(q) > 80:
                self._json({"error": "Search query is too long."}, 400)
                return
            try:
                self._json({"results": SERVICE.search(q)})
            except Exception as exc:
                ref = log_internal_error("search", exc)
                self._json({"error": "Search is temporarily unavailable.", "errorId": ref}, 502)
            return

        match = re.match(r"^/api/company/([^/]+)$", path)
        if match:
            symbol = match.group(1).upper()
            if not re.match(r"^[A-Z0-9.\-]{1,12}$", symbol):
                self._json({"error": "That does not look like a ticker symbol."}, 400)
                return
            try:
                self._json(SERVICE.bundle(symbol))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except FetchError as exc:
                self._json({"error": str(exc), "symbol": symbol}, 404)
            except Exception as exc:
                ref = log_internal_error("company", exc)
                self._json({"error": "Could not load that company.", "errorId": ref}, 500)
            return

        if path.startswith("/api/"):
            self._json({"error": "Unknown API route"}, 404)
            return
        self._serve_static(path)


def port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def main():
    parser = argparse.ArgumentParser(description="Ledger — governance & corporate issues")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    env_port = os.environ.get("PORT", "").strip()
    env_host = os.environ.get("HOST", "").strip()
    cloud = bool(env_port) or env_host == "0.0.0.0"
    host = args.host or env_host or ("0.0.0.0" if cloud else "127.0.0.1")
    try:
        port = args.port or int(env_port or 8770)
    except ValueError:
        raise SystemExit("PORT must be a number")

    if cloud:
        # A hosting platform routes traffic to exactly this port. Moving to
        # another one would leave the app unreachable, so fail loudly instead.
        if not port_available(host, port):
            raise SystemExit(f"Port {port} is already in use")
    else:
        for candidate in range(port, port + 25):
            if port_available(host, candidate):
                port = candidate
                break
        else:
            raise SystemExit(f"No free port found in range {port}-{port + 24}")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    url = f"http://{host}:{port}"
    print()
    print("  LEDGER — company governance & corporate issues")
    print(f"  {url}")
    print(f"  curated companies: {len((SERVICE.curated().get('companies') or {}))}")
    print("  Press Ctrl+C to stop.")
    print(flush=True)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
