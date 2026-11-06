# Security

Sawit is a read-only viewer for Malaysian listed companies. It stores no accounts, no user data and no payment information. This document states what it holds and what protects a public deployment.

## There are no API keys

Sawit needs **no API key, token or password** to run. It reads public market-data endpoints and links to Bursa Malaysia filings.

Verify it yourself:

```powershell
Get-ChildItem -Recurse -File -Include *.py,*.js,*.html,*.json,*.md |
  Select-String -Pattern 'api[_-]?key|secret|token|password|bearer|sk-|ghp_|AKIA'
```

Every match is documentation prose explaining that none are needed. There is no `.env` file and no hard-coded credential.

## Optional password

| Variable | Effect |
|---|---|
| `SAWIT_PASSWORD` | When set, every request needs HTTP Basic credentials |
| `SAWIT_USERNAME` | Username for that login (default `sawit`) |

```bash
SAWIT_PASSWORD='choose-something-long' SAWIT_USERNAME='me' python server.py
```

Comparison is constant-time. Leave it unset for frictionless local use.

## Enabled by default

| Control | Behaviour |
|---|---|
| **Rate limiting** | 120 requests/minute per client with `429` and `Retry-After`. Stops the public URL being scripted as a free proxy against the market-data source. Adjust with `SAWIT_RATE_LIMIT` (10–5000). |
| **Security headers** | Strict CSP with no inline scripts, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, device APIs disabled, cross-origin isolation. Google Fonts explicitly allowed. |
| **No version disclosure** | The `Server` header reads `Sawit` with no version number. |
| **Error redaction** | Exceptions are logged server-side with a short reference; visitors get a plain message and that reference. |
| **Input validation** | Bursa codes validated against a strict pattern; search capped at 80 characters; comparison limited to 5 symbols. |
| **No directory listing** | Unknown paths return `404`; static serving is confined to `static/`. |
| **No CORS** | No `Access-Control-Allow-Origin` header is sent. |
| **No write endpoints** | The server answers `GET` only. |

## Before publishing

- [ ] No `.env` file in the repository
- [ ] Environment variables set in the platform dashboard, not committed
- [ ] Optional: `SAWIT_PASSWORD` set if the link should not be public
- [ ] `/api/health` returns only status, version and a timestamp

## Not covered

- **Upstream trust.** Prices and ratios come from a public data source and can lag official filings. Report links always point at the authoritative document.
- **ESG.** Sawit provides a disclosure checklist, not vendor scores, because rating agencies disagree substantially with each other.
