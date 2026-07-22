# Data bootstrap report — 2026-07-17

This report records observed downloads, not strategy performance.

## Accepted

| Dataset ID | Rows | Scope |
|---|---:|---|
| `nasdaq_trader.current_symbols-20260717T030143Z-6e332ea97416` | 13,054 | Current symbol directory; not a historical universe |
| `exchange_calendar.xnys-20260717T030502Z-a6bb5767b8eb` | 1,004 | NYSE sessions from 2024-01-01 through 2027-12-31 |
| `alpaca.sip.adjusted-20260717T063910Z-124fbc358f9b` | 390 | AAPL adjusted SIP RTH minute bars for 2025-01-02 |
| `alpaca.sip.adjusted-20260717T064047Z-2f8a934c7440` | 78,520 | AAPL, SPY, UPWK, and AEHR all-session bars across 30 XNYS sessions |
| `data_quality.rth_coverage-20260717T064218Z-9e711bdda427` | 120 | Per-symbol/session RTH coverage audit derived from the Alpaca snapshot |
| `massive.sip.adjusted-20260717T074355Z-1bbc2ff94f0a` | 390 | Massive AAPL split-adjusted SIP RTH bars for 2025-01-02 |
| `alpaca.sip.adjusted-20260717T074509Z-a4a30dea8bcf` | 390 | Alpaca cross-check using the same split-only adjustment convention |
| `massive.grouped_daily.window30-20260717T082742Z-e3e1da342a7a` | 370,916 | Combined lineage snapshot for 30 individually accepted grouped-daily sessions |

## Quarantined by policy

| Dataset ID | Rows | Critical reason |
|---|---:|---|
| `yahoo.chart-20260717T030202Z-3906741d83de` | 26,718 | Undocumented feed/adjustment provenance; four incomplete second-aligned tail bars |
| `huggingface.crypto_spartan-20260717T030235Z-be7f319b6410` | 4,909 | Requested SPY was absent; redistribution/license provenance not approved |

The quarantined files prove the adapter and quality path works, but cannot support a
return, hit-rate, or stock-selection accuracy claim.

## Cross-provider check

After aligning Alpaca to split-only adjustment, Massive and Alpaca produced the same
390 timestamps. Low prices matched exactly; open/high/close differed on at most two
bars by no more than $0.0001. Volume differed on three minutes by 672,393 shares in
absolute total, and provider VWAP differed on every minute (maximum $0.140818), which
indicates different qualifying-trade aggregation rules. Research must therefore use
Massive as the primary OHLCV/VWAP source and Alpaca only as an independent coverage
and price cross-check; fields from the two vendors must not be mixed within a run.

## Grouped-daily bootstrap

The first 30 sessions span 2026-06-03 through 2026-07-16. Daily row counts range from
12,244 to 12,477, with 13,214 distinct symbols across the union and no duplicate
`(symbol, trade_date)` keys. Comparing the first and last session shows 618 symbols
appeared and 463 disappeared, demonstrating that the observed historical pool is not
a static copy of today's directory.

The resumable two-year backfill from 2024-07-17 through 2026-07-16 completed via
`scripts/backfill_massive_daily.ps1`. It contains 501 individually accepted sessions,
5,677,558 rows, and 15,813 distinct historical symbols in 169,478,403 compressed
Parquet bytes. Final validation found no missing or unexpected XNYS dates, duplicate
`(symbol, trade_date)` keys, invalid OHLC rows, or negative volume rows. The completion
log is in `runs/massive_daily_backfill.out.log`; the error log is empty.

## Credential boundary

Alpaca and Massive credentials are present and verified locally. SEC endpoints do not
require a key, but automated access still requires an identifying user agent containing
the owner's monitored email address.
