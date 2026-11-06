#!/usr/bin/env python3
"""
Sawit — Malaysian listed companies, their reports, and their ESG record.

Search any Bursa Malaysia-listed stock and see:
  * a financial snapshot (price, valuation, dividend, risk)
  * the reports that matter, linked straight to Bursa and the company's own
    investor-relations page, where the official Annual Report, Corporate
    Governance Report and Sustainability Report live
  * a governance panel built from disclosed officer data
  * a side-by-side comparison of up to five companies
  * an ESG disclosure guide: what to look for and where to find it

Why links rather than stored PDFs: Bursa Malaysia blocks automated access and
files are large. Sawit points you at the authoritative document instead of
mirroring a copy that could silently go stale.

Run:  python server.py
Open: http://127.0.0.1:8780
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
COMPANIES_PATH = DATA_DIR / "bursa_companies.json"
VERSION = "1.0.0"

PROFILE_TTL = 30 * 60
SEARCH_TTL = 30 * 60
DISK_TTL = 7 * 24 * 60 * 60

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}\.KL$|^[A-Z0-9]{1,10}$")
CODE_RE = re.compile(r"^(\d{1,5})(?:\.KL)?$", re.I)

# Sectors where conventional ratios mean different things.
FINANCIAL_SECTORS = {"financial services"}

# A practical ESG checklist for a Bursa-listed company. Each item names the
# document that should contain the answer, so the reader knows where to look.
ESG_CHECKLIST = [
    {
        "pillar": "Environment",
        "items": [
            {
                "label": "Scope 1 & 2 emissions",
                "what": "Direct and purchased-energy emissions, in tCO2e.",
                "where": "Sustainability Report, 'Climate change' section",
            },
            {
                "label": "Scope 3 emissions",
                "what": "Value-chain emissions. Often partial or omitted.",
                "where": "Sustainability Report, GHG inventory table",
            },
            {
                "label": "Emission-intensity target",
                "what": "A dated, quantified reduction target, not just an aspiration.",
                "where": "Sustainability Report, targets table",
            },
            {
                "label": "Energy and water use",
                "what": "Consumption in absolute terms and per unit of output.",
                "where": "Sustainability Report, performance data",
            },
            {
                "label": "Biodiversity and land use",
                "what": "Relevant for plantations, property and utilities.",
                "where": "Sustainability Report, environment section",
            },
        ],
    },
    {
        "pillar": "Social",
        "items": [
            {
                "label": "Workplace injury rates",
                "what": "Lost-time injury frequency rate, with year-on-year trend.",
                "where": "Sustainability Report, health & safety",
            },
            {
                "label": "Gender diversity at board and management",
                "what": "Percentage of women on the board and in senior roles.",
                "where": "Annual Report, board profile; CG Report",
            },
            {
                "label": "Employee turnover",
                "what": "Voluntary attrition, split by category if disclosed.",
                "where": "Sustainability Report, human capital",
            },
            {
                "label": "Supply-chain labour standards",
                "what": "Supplier audits, forced-labour risk, grievance mechanisms.",
                "where": "Sustainability Report, supply chain",
            },
            {
                "label": "Community investment",
                "what": "Spend and outcome measures, not just activities run.",
                "where": "Sustainability Report, community section",
            },
        ],
    },
    {
        "pillar": "Governance",
        "items": [
            {
                "label": "Board independence",
                "what": "Share of independent directors; Bursa practice favours a majority for large issuers.",
                "where": "Corporate Governance Report, board composition",
            },
            {
                "label": "CEO / chair separation",
                "what": "Whether one person holds both roles.",
                "where": "Corporate Governance Report; officer list above",
            },
            {
                "label": "Board diversity policy",
                "what": "A stated target, not only a stated intent.",
                "where": "Corporate Governance Report, nomination matters",
            },
            {
                "label": "Audit-committee independence",
                "what": "Composition and meeting frequency.",
                "where": "Corporate Governance Report, audit committee report",
            },
            {
                "label": "Related-party transactions",
                "what": "Recurrent related-party transactions and how they were approved.",
                "where": "Annual Report, RPT disclosures; circulars to shareholders",
            },
            {
                "label": "Sustainability assurance",
                "what": "Whether the sustainability report has independent limited assurance.",
                "where": "Sustainability Report, assurance statement",
            },
        ],
    },
]

POPULAR = [
    ("1155.KL", "Malayan Banking Berhad", "Financial Services"),
    ("1295.KL", "Public Bank Berhad", "Financial Services"),
    ("5347.KL", "Tenaga Nasional Berhad", "Utilities"),
    ("1023.KL", "CIMB Group Holdings Berhad", "Financial Services"),
    ("5225.KL", "IHH Healthcare Berhad", "Healthcare"),
    ("6947.KL", "CelcomDigi Berhad", "Communication Services"),
    ("4707.KL", "Nestlé (Malaysia) Berhad", "Consumer Defensive"),
    ("8869.KL", "Press Metal Aluminium Holdings Berhad", "Basic Materials"),
    ("5285.KL", "SD Guthrie Berhad", "Consumer Defensive"),
    ("7113.KL", "Top Glove Corporation Bhd", "Healthcare"),
    ("5180.KL", "Sunway Berhad", "Industrials"),
    ("6012.KL", "Maxis Berhad", "Communication Services"),
    ("4863.KL", "Telekom Malaysia Berhad", "Communication Services"),
    ("1961.KL", "IOI Corporation Berhad", "Consumer Defensive"),
    ("0102.KL", "MAG Holdings Berhad", "Consumer Defensive"),
    ("5258.KL", "Bank Islam Malaysia Berhad", "Financial Services"),
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
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_payload(value):
    """Replace non-finite floats with None without dropping keys."""
    if isinstance(value, dict):
        return {k: clean_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_payload(v) for v in value]
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


class FetchError(RuntimeError):
    pass


class Yahoo:
    """Quote, profile and officer data from Yahoo's public endpoints."""

    MODULES = "assetProfile,summaryDetail,financialData,defaultKeyStatistics,price"

    def __init__(self):
        self._lock = threading.Lock()
        self._opener = None
        self._crumb = None
        self._born = 0.0
        self._search = {}

    def _session(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.addheaders = [
            ("User-Agent", UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]
        with opener.open("https://finance.yahoo.com/quote/1155.KL/", timeout=20) as r:
            r.read(64_000)
        crumb = opener.open(
            "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15
        ).read().decode("utf-8", "replace").strip()
        if not crumb or len(crumb) > 40:
            raise FetchError("Yahoo session could not be established")
        self._opener = opener
        self._crumb = crumb
        self._born = time.time()

    def _reset(self):
        self._opener = None
        self._crumb = None
        self._born = 0.0

    def _json(self, url, timeout=25):
        opener = self._opener
        if opener is None:
            opener = urllib.request.build_opener()
            opener.addheaders = [("User-Agent", UA)]
        with opener.open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def profile(self, symbol: str):
        last = None
        for _ in range(2):
            with self._lock:
                try:
                    if self._opener is None or time.time() - self._born > 45 * 60:
                        self._reset()
                        self._session()
                    encoded = urllib.parse.quote(symbol, safe="")
                    crumb = urllib.parse.quote(self._crumb or "", safe="")
                    url = (
                        "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
                        f"{encoded}?modules={self.MODULES}&crumb={crumb}"
                    )
                    data = self._json(url)
                    result = (data.get("quoteSummary", {}).get("result") or [None])[0]
                    if not result:
                        raise FetchError("Empty profile")
                    return result
                except urllib.error.HTTPError as exc:
                    last = exc
                    self._reset()
                except urllib.error.URLError as exc:
                    last = exc
                    self._reset()
                except (TimeoutError, OSError, ValueError, FetchError) as exc:
                    last = exc
                    self._reset()
            time.sleep(1.0)
        raise FetchError(f"Could not load {symbol}: {last}")

    def search(self, query: str):
        key = query.strip().lower()
        cached = self._search.get(key)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
        params = urllib.parse.urlencode(
            {"q": query, "quotesCount": 25, "newsCount": 0}
        )
        try:
            data = self._json(f"https://query1.finance.yahoo.com/v1/finance/search?{params}")
        except Exception:
            data = {}
        out = []
        for q in data.get("quotes", []) or []:
            if q.get("quoteType") not in ("EQUITY", "ETF"):
                continue
            symbol = str(q.get("symbol", ""))
            if not symbol.endswith(".KL"):
                continue
            out.append(
                {
                    "symbol": symbol,
                    "code": symbol.replace(".KL", ""),
                    "name": q.get("longname") or q.get("shortname") or symbol,
                    "exchange": q.get("exchDisp") or "Bursa Malaysia",
                    "sector": q.get("sectorDisp") or q.get("sector") or "",
                }
            )
        self._search[key] = (time.time(), out)
        return out

    def quote_line(self, symbol: str):
        encoded = urllib.parse.quote(symbol, safe="")
        data = self._json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
            "?range=1d&interval=1d",
            timeout=18,
        )
        result = (data.get("chart", {}).get("result") or [None])[0]
        return (result or {}).get("meta") or {}

    def chart_points(self, symbol: str):
        encoded = urllib.parse.quote(symbol, safe="")
        data = self._json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
            "?range=1y&interval=1d",
            timeout=10,
        )
        result = (data.get("chart", {}).get("result") or [None])[0] or {}
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators", {}).get("quote") or [{}])[0]) or {}
        closes = quote.get("close") or []
        pts = [
            {"t": int(t) * 1000, "c": round(float(c), 3)}
            for t, c in zip(stamps, closes)
            if c is not None
        ]
        if len(pts) > 140:
            stride = max(1, len(pts) // 130)
            sampled = pts[::stride]
            if sampled[-1] is not pts[-1]:
                sampled.append(pts[-1])
            pts = sampled
        return pts


YAHOO = Yahoo()


class Service:
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._directory = None

    # ---------- helpers ----------
    @staticmethod
    def _disk_path(symbol):
        return CACHE_DIR / f"{symbol.replace('/', '_')}.json"

    def _disk_read(self, symbol):
        p = self._disk_path(symbol)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
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

    def directory(self):
        with self._lock:
            if self._directory is not None:
                return self._directory
            try:
                self._directory = json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
            except Exception:
                self._directory = {"companies": {}}
            return self._directory

    @staticmethod
    def normalise_symbol(raw: str) -> str:
        raw = (raw or "").strip().upper()
        if not raw:
            raise ValueError("Enter a Bursa stock code or ticker")
        if raw.endswith(".KL"):
            base = raw[:-3]
            if not re.match(r"^[A-Z0-9]{1,10}$", base):
                raise ValueError("Invalid ticker")
            return f"{base}.KL"
        m = CODE_RE.match(raw)
        if m:
            return f"{m.group(1)}.KL"
        if SYMBOL_RE.match(raw):
            return f"{raw}.KL"
        raise ValueError("Enter a Bursa stock code, e.g. 1155 or MAYBANK")

    # ---------- report links ----------
    @staticmethod
    def report_links(symbol: str, company: dict) -> list:
        code = symbol.replace(".KL", "")
        name = company.get("name") or ""
        website = company.get("website") or ""
        links = [
            {
                "kind": "Bursa announcements",
                "label": "Bursa Malaysia announcements",
                "url": (
                    "https://www.bursamalaysia.com/market_information/announcements/"
                    f"company_announcement?company={code}"
                ),
                "note": "Official filings: annual report, quarterly results, material announcements.",
                "primary": True,
            },
            {
                "kind": "Annual reports index",
                "label": "Annual & CG reports index",
                "url": f"https://www.malaysiastock.biz/Annual-Report.aspx?securityCode={code}",
                "note": "PDF copies of Annual Reports and Corporate Governance Reports where archived.",
                "primary": True,
            },
            {
                "kind": "Company profile",
                "label": "Company information",
                "url": f"https://www.malaysiastock.biz/Corporate-Infomation.aspx?securityCode={code}",
                "note": "Shareholding, board and corporate information.",
                "primary": False,
            },
        ]
        if website:
            base = website.rstrip("/")
            links.append(
                {
                    "kind": "Investor relations",
                    "label": "Investor relations",
                    "url": base,
                    "note": "The company's own IR page — usually the fastest route to the latest report.",
                    "primary": True,
                }
            )
        return links

    # ---------- governance summary ----------
    @staticmethod
    def governance(company: dict) -> dict:
        officers = company.get("officers") or []
        if not officers:
            return {"available": False, "facts": [], "officers": []}
        ceo = [o for o in officers if o.get("isCEO")]
        chairs = [o for o in officers if o.get("isChair")]
        combined = [o for o in officers if o.get("isCEO") and o.get("isChair")]
        paid = [o for o in officers if o.get("pay")]
        ages = [o["age"] for o in officers if o.get("age")]

        facts = []
        facts.append(f"{len(officers)} executives disclosed in the company profile.")
        if combined:
            facts.append("One person holds both the CEO and chair roles.")
        elif ceo and chairs:
            facts.append("CEO and chair roles are held by different people.")
        if paid:
            top = max(paid, key=lambda o: o["pay"])
            facts.append(
                f"{len(paid)} of {len(officers)} listed executives have disclosed pay; "
                f"the highest is {top['name']} at RM {top['pay']:,.0f}."
            )
        else:
            facts.append(
                "Individual executive pay is not published in this data source; "
                "it appears in the Annual Report's remuneration table."
            )
        if ages:
            avg = sum(ages) / len(ages)
            facts.append(f"Average disclosed age of leadership is {avg:.0f}.")
        return {
            "available": True,
            "facts": facts,
            "ceo": ceo[0]["name"] if ceo else None,
            "chair": chairs[0]["name"] if chairs else None,
            "combinedRoles": bool(combined),
            "officers": officers,
        }

    # ---------- public ----------
    def search(self, query: str):
        q = (query or "").strip()
        if not q:
            return []
        results = YAHOO.search(q)
        if results:
            return results
        # Fall back to matching the bundled directory by name or code.
        needle = q.lower().replace(" ", "")
        out = []
        for code, row in (self.directory().get("companies") or {}).items():
            name = str(row.get("name", ""))
            if needle in code.lower() or needle in name.lower().replace(" ", ""):
                out.append(
                    {
                        "symbol": f"{code}.KL",
                        "code": code,
                        "name": name,
                        "exchange": "Bursa Malaysia",
                        "sector": row.get("sector", ""),
                    }
                )
            if len(out) >= 20:
                break
        return out

    def popular(self):
        return [
            {"symbol": s, "code": s.replace(".KL", ""), "name": n, "sector": sec}
            for s, n, sec in POPULAR
        ]

    def company(self, raw_symbol: str):
        symbol = self.normalise_symbol(raw_symbol)
        with self._lock:
            hit = self._cache.get(symbol)
            if hit and time.time() - hit[0] < PROFILE_TTL:
                return hit[1]

        data = YAHOO.profile(symbol)
        profile = data.get("assetProfile") or {}
        summary = data.get("summaryDetail") or {}
        fin = data.get("financialData") or {}
        stats = data.get("defaultKeyStatistics") or {}
        price = data.get("price") or {}

        currency = price.get("currency") or "MYR"
        officers = []
        for o in profile.get("companyOfficers") or []:
            title = (o.get("title") or "").strip()
            boardish = bool(re.search(r"Chairman|Chair|Board|Non Independent", title, re.I))
            officers.append(
                {
                    "name": re.sub(r"^(Mr\.|Ms\.|Mrs\.|Dr\.)\s*", "", (o.get("name") or "").strip()),
                    "title": title,
                    "age": safe_float(o.get("age")),
                    "pay": safe_float((o.get("totalPay") or {}).get("raw")),
                    "isCEO": bool(re.search(r"\bCEO\b|Chief Executive", title, re.I)),
                    "isChair": bool(re.search(r"Chairman|Chair", title, re.I)),
                    "isCFO": bool(re.search(r"\bCFO\b|Chief Financial", title, re.I)),
                    "boardish": boardish,
                }
            )

        points = []
        meta = {}
        # The profile already carries the live price, so history is optional
        # extra detail. Fetch it with a short budget and never block the page.
        try:
            points = YAHOO.chart_points(symbol)
        except Exception:
            points = []

        last = safe_float(price.get("regularMarketPrice")) or (
            points[-1]["c"] if points else None
        )
        prev = safe_float(price.get("regularMarketPreviousClose")) or safe_float(
            meta.get("chartPreviousClose")
        )
        change_pct = None
        if last and prev:
            change_pct = last / prev - 1

        shares = safe_float(stats.get("sharesOutstanding"))
        market_cap = safe_float(price.get("marketCap")) or (
            last * shares if last and shares else None
        )
        sector = profile.get("sector") or ""
        is_financial = sector.lower() in FINANCIAL_SECTORS

        payload = {
            "symbol": symbol,
            "code": symbol.replace(".KL", ""),
            "name": price.get("longName") or price.get("shortName") or symbol,
            "shortName": price.get("shortName") or symbol,
            "exchange": price.get("exchangeName") or "Bursa Malaysia",
            "currency": currency,
            "sector": sector,
            "industry": profile.get("industry") or "",
            "country": profile.get("country") or "Malaysia",
            "city": profile.get("city") or "",
            "website": profile.get("website") or "",
            "employees": safe_float(profile.get("fullTimeEmployees")),
            "description": profile.get("longBusinessSummary") or "",
            "isFinancial": is_financial,
            "price": {
                "last": last,
                "previousClose": prev,
                "changePercent": change_pct,
                "low52": safe_float(summary.get("fiftyTwoWeekLow")),
                "high52": safe_float(summary.get("fiftyTwoWeekHigh")),
                "marketCap": market_cap,
                "history": points,
            },
            "financials": {
                "pe": safe_float(summary.get("trailingPE")),
                "forwardPe": safe_float(summary.get("forwardPE")),
                "eps": safe_float(stats.get("trailingEps")),
                "forwardEps": safe_float(stats.get("forwardEps")),
                "bookValue": safe_float(stats.get("bookValue")),
                "priceToBook": safe_float(stats.get("priceToBook")),
                "dividendRate": safe_float(summary.get("dividendRate")),
                "dividendYield": safe_float(summary.get("dividendYield")),
                "payoutRatio": safe_float(summary.get("payoutRatio")),
                "beta": safe_float(summary.get("beta")),
                "grossMargin": safe_float(fin.get("grossMargins")),
                "operatingMargin": safe_float(fin.get("operatingMargins")),
                "profitMargin": safe_float(fin.get("profitMargins")),
                "returnOnEquity": safe_float(fin.get("returnOnEquity")),
                "returnOnAssets": safe_float(fin.get("returnOnAssets")),
                "revenueGrowth": safe_float(fin.get("revenueGrowth")),
                "totalRevenue": safe_float(fin.get("totalRevenue")),
                "totalCash": safe_float(fin.get("totalCash")),
                "totalDebt": safe_float(fin.get("totalDebt")),
                "debtToEquity": safe_float(fin.get("debtToEquity")),
                "currentRatio": safe_float(fin.get("currentRatio")),
                "shares": shares,
            },
            "reports": self.report_links(symbol, {
                "name": price.get("longName"),
                "website": profile.get("website"),
            }),
            "governance": None,
            "esgChecklist": ESG_CHECKLIST,
            "meta": {"provider": "Yahoo Finance · Bursa Malaysia", "fetchedAt": utc_now()},
        }
        # Attach officers first, then summarise governance from them.
        payload["officers"] = officers
        payload["governance"] = self.governance(payload)
        payload = clean_payload(payload)
        with self._lock:
            self._cache[symbol] = (time.time(), payload)
            self._disk_write(symbol, payload)
        return payload

    def compare(self, symbols):
        cleaned = []
        for s in symbols:
            try:
                cleaned.append(self.normalise_symbol(s))
            except ValueError:
                continue
        cleaned = cleaned[:5]
        if not cleaned:
            raise ValueError("Provide at least one Bursa stock code")

        results = {}
        with ThreadPoolExecutor(max_workers=min(5, len(cleaned))) as pool:
            futures = {pool.submit(self.company, s): s for s in cleaned}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results[sym] = future.result()
                except Exception:
                    continue
        ordered = [results[s] for s in cleaned if s in results]
        if not ordered:
            raise FetchError("None of those tickers could be loaded")
        return {"companies": ordered}


SERVICE = Service()


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
# Sawit holds no API keys, tokens or personal data: it reads public market data
# and links to Bursa filings, storing nothing about visitors. The controls
# below address what publishing it actually exposes: proxy abuse, internal
# detail leaking through errors or the version header, and cross-site framing.

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

AUTH_PASSWORD = os.environ.get("SAWIT_PASSWORD", "").strip()
AUTH_USER = os.environ.get("SAWIT_USERNAME", "sawit").strip() or "sawit"


class RateLimiter:
    """Fixed-window limiter so a public link cannot be scripted as a proxy."""

    def __init__(self, window=60):
        try:
            self.limit = int(os.environ.get("SAWIT_RATE_LIMIT", 120))
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
    server_version = "Sawit"
    sys_version = ""

    def version_string(self):
        return self.server_version

    def end_headers(self):
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def log_message(self, fmt, *args):
        if os.environ.get("SAWIT_QUIET") == "1":
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
        self.send_header("WWW-Authenticate", 'Basic realm="Sawit", charset="UTF-8"')
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

    def do_HEAD(self):
        self.do_GET()

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
            self._json({"ok": True, "version": VERSION, "time": utc_now()})
            return

        if path == "/api/popular":
            self._json({"results": SERVICE.popular()})
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

        if path == "/api/esg":
            self._json({"checklist": ESG_CHECKLIST})
            return

        if path == "/api/compare":
            raw = (query.get("symbols") or [""])[0]
            symbols = [s for s in re.split(r"[,\s]+", raw) if s][:5]
            try:
                self._json(SERVICE.compare(symbols))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except FetchError as exc:
                self._json({"error": str(exc)}, 404)
            except Exception as exc:
                ref = log_internal_error("compare", exc)
                self._json({"error": "Could not compare those companies.", "errorId": ref}, 500)
            return

        match = re.match(r"^/api/company/([^/]+)$", path)
        if match:
            raw = match.group(1)
            if not re.match(r"^[A-Z0-9]{1,10}(\.KL)?$", raw.upper()):
                self._json({"error": "That does not look like a Bursa stock code."}, 400)
                return
            try:
                self._json(SERVICE.company(raw))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except FetchError as exc:
                self._json({"error": str(exc)}, 404)
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
    parser = argparse.ArgumentParser(description="Sawit — Malaysian stock reports & ESG")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    env_port = os.environ.get("PORT", "").strip()
    env_host = os.environ.get("HOST", "").strip()
    cloud = bool(env_port) or env_host == "0.0.0.0"
    host = args.host or env_host or ("0.0.0.0" if cloud else "127.0.0.1")
    try:
        port = args.port or int(env_port or 8780)
    except ValueError:
        raise SystemExit("PORT must be a number")

    if cloud:
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
    print("  SAWIT — Malaysian stocks, reports & ESG")
    print(f"  {url}")
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
