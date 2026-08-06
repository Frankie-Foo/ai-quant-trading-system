# Premarket RVOL audit — 2026-07-20

## Decision contract

The catalyst pool is frozen to the 97 symbols in accepted snapshot
`kernel.catalysts.overnight_candidates-20260720T080424Z-588747108000`.
Premarket verification cannot introduce a new symbol.

For target session `t`, the feature is:

```text
RVOL(t) = volume(t, [04:00 ET, cutoff ET))
          / median(volume(t-i, [04:00 ET, cutoff ET)), i=1..20)
```

The gate is strictly `RVOL > 3.0`. The interval is half-open. With a Beijing 20:00
decision and a 15-minute data delay on 2026-07-20, the cutoff is 07:45 ET; the last
eligible one-minute bar is stamped 07:44 ET. Bars stamped 07:45 or later cannot affect
the result.

Each historical session is converted independently from New York wall time to UTC,
so daylight-saving transitions preserve a genuine same-time comparison.

## Missing-data policy

Alpaca documents that no stock minute bar is generated when an interval has no
qualifying trade. Therefore, after a successful and fully paginated session request,
an absent bar contributes zero qualifying volume. This is aggregation, not a forward
fill: no OHLC value or prior volume is copied into a missing minute.

A failed or incomplete session request is not treated as zero. Any missing request
among the current session or prior 20 sessions makes the feature unavailable. A zero
historical median is also undefined and fails closed instead of producing infinity.

Reference: [Alpaca Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).

## History prefetch evidence

The first run completed at 2026-07-20 before the decision time and correctly stopped
without creating a target feature snapshot.

- locked symbols: 97
- accepted historical session snapshots: 20
- history range: 2026-06-18 through 2026-07-17
- canonical one-minute bars: 56,822
- symbols with at least one emitted premarket bar: 93
- duplicate `(symbol, ts_utc)` keys: 0
- failed critical data checks: 0
- target query not before: 2026-07-20 20:00 Asia/Shanghai

The final target-date counts and RVOL survivors remain intentionally pending until the
decision time. The operational script reuses these immutable history snapshots on its
next run.

## No-lookahead tests

Seven deterministic tests prove that:

1. injecting target bars at or after the cutoff cannot change RVOL;
2. historical post-cutoff and full-day volumes are excluded;
3. the denominator uses exactly the prior 20 same-time sessions;
4. a missing provider session fails closed;
5. a zero historical median is unavailable rather than infinite;
6. DST offsets are derived independently per session; and
7. bars for symbols outside the locked pool cannot enter the result.
