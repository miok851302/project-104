# Sawit — Malaysian stocks, reports & ESG

**Sawit** is the Malay word for palm oil, Malaysia's most famous crop. This app is a lens on Bursa Malaysia: search any listed company, see its numbers, and get straight to its official reports.

![Sawit](screens/company.png)

## Quick start

**Windows:** double-click `start.bat`.

**Any OS with Python 3.10+:**

```bash
python server.py --open
```

Open <http://127.0.0.1:8780>. No API key, no account, no package install — the server uses only Python's standard library.

## What you get

### 1. Search any Bursa company
Type a code (`1155`), a ticker (`MAYBANK`), or a word from the name. Live suggestions as you type, plus one-click chips for 16 well-known listings across MAIN, ACE and LEAP markets.

### 2. Financial snapshot
Live price and a one-year chart, then sixteen valuation and quality measures in one grid: market cap, trailing and forward P/E, EPS, dividend yield, dividend per share, payout ratio, price-to-book, book value, ROE, ROA, net and operating margins, revenue growth, beta and share count.

### 3. Reports — the real ones
Four links per company, each opening the authoritative document or its index:

| Link | What you find |
|---|---|
| **Bursa Malaysia announcements** | The official filings: annual report, quarterly results, material announcements |
| **Annual & CG reports index** | Archived PDF copies of Annual Reports and Corporate Governance Reports |
| **Company information** | Shareholding, board composition and corporate data |
| **Investor relations** | The company's own IR page — usually the fastest route to the newest report |

**Why links and not stored copies:** Bursa blocks automated access, and reports run to hundreds of megabytes. Sawit points you at the authoritative source instead of mirroring a file that could silently go stale. What you open is exactly what the company filed.

### 4. Governance facts
The disclosed leadership team — names, titles, ages and disclosed pay — plus plain statements such as whether one person holds both the CEO and chair roles. **Facts only; Sawit does not rate people.**

### 5. Compare up to five companies
Enter `1155, 1295, 5347` and get the same measures side by side. Differences become obvious: Tenaga's revenue growth against the banks, margin structures, dividend policies.

### 6. ESG guide
Sixteen checks across Environment, Social and Governance. For each one, Sawit names **which document should contain the answer** — so you can verify a sustainability report rather than skim it.

This matters because Bursa requires sustainability reporting but depth varies enormously. A report that omits Scope 3 emissions or gives no dated reduction target is telling you something, and this list lets you see it.

## Honest limitations

- **No ESG scores.** Vendor ratings (MSCI, Sustainalytics, S&P) disagree substantially with each other, and the reliable ones are paid. Sawit gives you the disclosure checklist and the documents; the judgement stays yours.
- **Data lag.** Prices come from public endpoints and update through the trading day, but ratios and margins are only as fresh as the source and can trail official filings. Always confirm material numbers in the annual report.
- **Coverage.** Malaysian companies whose ticker resolves on the data source work. Some thinly traded ACE and LEAP counters may not resolve; Sawit says so plainly rather than guessing.
- **Not investment advice.** Sawit is a filing finder and a ratio table. It does not know your circumstances.

## Structure

```
sawit/
├── start.bat / start.ps1
├── server.py              # HTTP server, data client, report links, ESG checklist
├── static/
│   ├── index.html
│   ├── css/styles.css
│   └── js/app.js
├── cache/                 # saved responses (7 days), created automatically
└── screens/
```

## API

| Route | Purpose |
|---|---|
| `/healthz` | Plain `ok`, for platform probes |
| `/api/health` | Version and status |
| `/api/search?q=maybank` | Company suggestions |
| `/api/company/1155` | Full company bundle |
| `/api/compare?symbols=1155,1295` | Up to five companies |
| `/api/esg` | The ESG checklist |
| `/api/popular` | Default chips |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | 8780 | Port to listen on |
| `HOST` | 127.0.0.1 | Bind address (`0.0.0.0` in containers) |
| `SAWIT_QUIET` | unset | Set to `1` to silence request logs |

Caching: profiles 30 minutes in memory, 7 days on disk; searches 15 minutes.

## Deploying

```bash
docker build -t sawit .
docker run -p 8000:8000 sawit
```

On Render: **New → Web Service → Docker → Free → Health Check Path `/healthz`**. See `DEPLOY.md`.

## Data

Prices, ratios and officer data come from Yahoo Finance's public endpoints. Report links point to Bursa Malaysia and each company's own investor-relations site.

Sawit is not affiliated with Bursa Malaysia, any company listed here, or Yahoo. All trademarks belong to their owners.
