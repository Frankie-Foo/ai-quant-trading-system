# Cloud feature interface

The multi-strategy platform is intentionally outside this repository. The two Git
repositories share no Python imports, source directories, virtual environments,
SQLite databases, runtime roots, or credentials.

## Allowed dependency direction

This repository may call only:

```text
GET /v1/features/{symbol}?asof=<UTC RFC3339>
Authorization: Bearer <CLOUD_FEATURE_API_TOKEN>
```

The response is a versioned point-in-time feature vector containing a definition
version and provenance for every value. Unsupported versions, invalid timestamps,
authorization failures, network failures, and schema drift all fail closed.

The cloud repository does not expose raw SIP, proxy, account, position, TradePlan,
Broker, or order endpoints. Collaborator signal tokens use a different scope and cannot
call the feature endpoint. The AI feature token cannot query collaborator-only signals.

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

The original single-strategy SIP and local observation paths remain independent and do
not require the cloud service to be available.
