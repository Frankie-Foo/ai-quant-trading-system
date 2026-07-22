# Cloud feature interface

The multi-strategy platform is intentionally outside this repository. The two Git
repositories share no Python imports, source directories, virtual environments,
SQLite databases, runtime roots, or credentials.

## Allowed dependency direction

This repository may call these versioned endpoints with separate service tokens:

```text
GET /v1/features/{symbol}?asof=<UTC RFC3339>
Authorization: Bearer <CLOUD_FEATURE_API_TOKEN>

GET /v1/market-data/events?after=<sequence>&symbols=AAPL,MSFT
Authorization: Bearer <CLOUD_MARKET_DATA_API_TOKEN>

GET /v1/market-data/bars|quotes|news
Authorization: Bearer <CLOUD_MARKET_DATA_API_TOKEN>

GET|POST|DELETE /v1/paper/...
Authorization: Bearer <CLOUD_PAPER_API_TOKEN>
```

The response is a versioned point-in-time feature vector containing a definition
version and provenance for every value. Unsupported versions, invalid timestamps,
authorization failures, network failures, and schema drift all fail closed.

The market API returns bounded normalized events and historical rows, not Alpaca
credentials or a generic upstream proxy. The Paper API is restricted to Paper accounts
and long-only contracts. Collaborator signal tokens cannot call any AI endpoint. Market,
feature, Paper, and signal scopes cannot be exchanged.

## Fast-loop safety

`CloudFeatureClient` is a slow-loop adapter. Run synchronization before a decision:

```powershell
$env:CLOUD_FEATURE_API_TOKEN = "<service-token>"
.\.venv\Scripts\python -m scripts.sync_cloud_features `
  --base-url https://features.internal.example `
  --symbols AAPL,MSFT --asof 2026-07-22T15:30:00Z
```

The command persists verified responses to `runs/cloud-feature-cache.sqlite3`.
Decision-time code reads `CloudFeatureCache.latest(...)`; it must never instantiate or
call `CloudFeatureClient`. If a synchronized vector is absent, the feature is `N/A`.

Realtime observation consumes cloud events into the existing local SIP store before
running ORB logic. A cloud outage produces no event and no order; it never causes a
fallback direct Alpaca connection.
