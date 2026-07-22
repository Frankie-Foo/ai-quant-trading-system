# Selection accuracy audit — 2026-07-20

## Scope

This milestone closes the deterministic gaps between the 97-symbol catalyst lock and
an auditable intraday signal. It does not claim that the strategy is profitable or that
an untrained model probability is accurate.

## Data gates

1. **Point-in-time market cap:** Massive ticker details are queried with the prior
   session date. Missing market cap fails closed because sizing cannot choose a tier.
2. **Free float:** Massive's latest free-float endpoint supplies effective date, shares,
   and percentage. Reporting lag remains explicit.
3. **Earnings day:** Nasdaq's calendar, whose page identifies Zacks as its provider,
   supplies expected report dates and timing. Nasdaq states that the list is derived
   algorithmically from historical reporting dates, so every listed symbol is excluded
   conservatively and the limitation is recorded.
4. **Halts and LULD:** Nasdaq Trader's free all-exchange RSS supplies current and
   historical halts. `LUDP` and `LUDS` are treated as LULD events. An unresolved halt
   remains current even when it began on a prior session.

The hard pass rule is:

```text
daily precheck
AND RVOL > 3
AND market cap is available
AND not an earnings-calendar symbol
AND not currently halted
AND NOT(recent five-session LULD AND free float < 20m or unavailable)
```

The 20 million-share threshold is a visible configuration value, not an undocumented
heuristic hidden in code.

Real reference acceptance at 2026-07-20 09:22 UTC found 97/97 positive market-cap
records for the prior session (2026-07-17), with no duplicate symbols. The current
free-float table returned 93/97 positive records, also without duplicates; DBVT, FABC,
PPLI, and VIVO were absent and are not imputed. The same run accepted 25 earnings rows
(zero locked-symbol overlap) and 284 unique halt events across the current and five
prior sessions. Target-date selection output remains unavailable until the 20:00
Beijing RVOL cutoff is complete.

## ORB-5 and labels

- opening range: `[09:30, 09:35 ET)`;
- the aggregated range must close above its first open;
- the first later bar whose high exceeds the range high triggers;
- historical entry is the next **complete** minute bar's VWAP;
- the trigger bar cannot price its own fill;
- all five opening-range minutes and the exact next entry minute must exist; missing
  bars fail closed and are never replaced with a later quote;
- each symbol can produce at most one entry per day;
- exits are `entry + 2×ATR`, `entry - 1×ATR`, or 15:55 ET (12:55 on half days);
- if one minute touches TP and SL, SL wins;
- a stop touched during the time-stop minute wins over that minute's close;
- any missing post-entry minute invalidates the replay instead of silently skipping
  a possible barrier touch;
- the entry bar is excluded from barrier scanning because its final VWAP was not known
  until that bar completed.

## Costs

Both legs include commission, full estimated spread, and square-root participation
impact. The sell leg adds SEC and FINRA TAF charges. A stop exit adds an adverse
`0.5×ATR` slippage amount. Configuration only permits conservative spread assumptions.

## Model safety

The deterministic kernel never calls an LLM. The slow-loop catalyst scorer accepts only
one bare probability, records model ID, prompt SHA-256, evidence IDs, and temperature
zero. The score remains unusable until an out-of-sample calibration marks it approved.
Evaluation must begin strictly after a defensible training-cutoff upper bound; when the
provider does not disclose one, only forward observations after release and fingerprint
registration qualify.

The selected provider adapter uses the exact API model `deepseek-v4-pro` in
non-thinking mode with temperature zero. It records the provider request ID, returned
model ID, system fingerprint, and token counts. Evidence text is treated as untrusted
quoted data, and the scorer resolves only event IDs frozen in the morning candidate
snapshot. Because DeepSeek does not publish an exact V4-Pro training cutoff, historical
scores are research features only; calibration evidence must be forward/OOS and tied to
the observed system fingerprint.

Purged walk-forward validation is chronological, never shuffled, and records the purge
and embargo boundaries. Historical Massive news is backfilled in restart-safe monthly
partitions; historical minute bars will be pulled only for reconstructed event
candidates and SPY rather than the full market.

## Live-data limitation

The current Alpaca Basic plan exposes real-time equities from IEX only and requires SIP
queries to end at least 15 minutes in the past. IEX is insufficient for an accuracy-
focused full-market breakout signal. Operational ORB output is therefore stamped
`delayed_shadow_only` and is barred from live orders until licensed real-time SIP is
available.

Primary references:

- [Massive ticker details](https://massive.com/docs/rest/stocks/tickers/ticker-overview)
- [Massive free float](https://massive.com/docs/rest/stocks/fundamentals/float)
- [Nasdaq earnings calendar](https://www.nasdaq.com/market-activity/earnings)
- [Nasdaq Trader halt RSS](https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS)
- [Nasdaq halt codes](https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltCodes)
- [Alpaca market-data plans](https://docs.alpaca.markets/us/v1.1/docs/about-market-data-api)
