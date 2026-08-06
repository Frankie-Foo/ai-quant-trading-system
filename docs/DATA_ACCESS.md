# Data access

Market data is ingested into immutable Parquet snapshots. A critical quality or
provenance failure sends the snapshot to `data/quarantine/`; downstream research must
consume only `data/accepted/`.

## Public bootstrap

These commands need no account credentials:

```powershell
.\.venv\Scripts\python -m data_plane.cli nasdaq-reference
.\.venv\Scripts\python -m data_plane.cli calendar --start 2024-01-01 --end 2027-12-31
.\.venv\Scripts\python -m data_plane.cli yahoo-staging --symbols AAPL,NVDA,QQQ,SPY --range 7d
.\.venv\Scripts\python -m data_plane.cli hf-staging `
  --symbols AAPL,NVDA,QQQ,SPY `
  --start 2025-01-02T00:00:00Z `
  --end 2025-01-04T00:00:00Z
```

Yahoo and community Hugging Face data are intentionally quarantined. They verify the
engineering path but are not admissible strategy-performance evidence.

## Credentials

Copy `.env.example` to `.env` and populate values locally. Never send keys in chat or
commit `.env`.

- `CLOUD_PLATFORM_BASE_URL` and `CLOUD_MARKET_DATA_API_TOKEN`: scoped access to the
  independent cloud service. Alpaca provider credentials exist only in that service.
  The API returns SIP, split-adjusted rows with provenance.
- `MASSIVE_API_KEY`: Massive Basic or a paid Stocks plan. The API adapter requests
  one-minute aggregates adjusted for splits, matching the frozen specification.
- `SEC_USER_AGENT`: a descriptive SEC user agent containing a monitored email address,
  for example `Frank research name@example.com`.

Check only whether the values are present, without printing secrets:

```powershell
.\.venv\Scripts\python -m data_plane.cli credentials
```

Download direct-source samples:

```powershell
.\.venv\Scripts\python -m data_plane.cli alpaca `
  --symbols AAPL,NVDA,QQQ,SPY `
  --start 2025-01-02T00:00:00Z `
  --end 2025-01-04T00:00:00Z

.\.venv\Scripts\python -m data_plane.cli massive `
  --symbols AAPL,NVDA,QQQ,SPY `
  --start 2025-01-02T00:00:00Z `
  --end 2025-01-04T00:00:00Z
```

Current Nasdaq and SEC directories are reference data only. They must never be used as
a historical point-in-time universe.

## Massive grouped-daily backfill

The grouped-daily downloader stores and validates one immutable snapshot per XNYS
session. Rerunning the command skips accepted dates, so an interruption resumes rather
than restarts:

```powershell
.\.venv\Scripts\python -m data_plane.cli massive-grouped-daily `
  --start 2024-07-17 --end 2026-07-16
```

The long-running wrapper retries a failed batch from its accepted checkpoints:

```powershell
.\scripts\backfill_massive_daily.ps1
```
