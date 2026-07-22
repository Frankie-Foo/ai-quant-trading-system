# Catalyst evidence audit — 2026-07-20

## Outcome

The first real overnight catalyst run completed for the 2026-07-20 XNYS session.
The evidence window was strictly bounded by the previous close and the Beijing 08:00
pool lock:

- window start: 2026-07-17 20:00:00 UTC (previous XNYS close)
- decision cutoff: 2026-07-20 00:00:00 UTC (Beijing 08:00)
- Alpaca/Benzinga raw articles: 101
- Massive raw articles: 180
- SEC candidate filings with exact acceptance timestamps: 69
- total raw evidence records: 350
- eligible evidence records after deterministic cleaning: 118
- daily-precheck symbols with at least one eligible event: 97
- symbols corroborated by at least two independent sources: 7
- populated model scores: 0
- critical quality failures: 0

The accepted candidate snapshot is
`kernel.catalysts.overnight_candidates-20260720T080424Z-588747108000`.
It is an evidence pool, not a buy list or a strategy-performance result.

## Real-data defects found and fixed

The original paper-derived rules removed 77 broad articles mentioning more than three
companies, 30 articles with fewer than 25 words, and one duplicate event chain. Real
data exposed three additional high-volume false-positive families:

- 73 Motley Fool editorial/opinion articles;
- 35 securities-law-firm solicitations and lead-plaintiff notices;
- 16 backward-looking “if you invested” performance articles.

These families now receive explicit exclusion reasons. They remain in the immutable
prepared snapshot for audit but cannot enter the eligible event pool.

## SEC point-in-time handling

The pipeline downloads one SEC daily master index, intersects it with CIKs from the
accepted common-stock daily pre-universe, and then queries submissions only for matched
issuers. The `acceptanceDateTime` field, not the filing date alone, determines whether a
filing existed before the lock cutoff.

8-K Item codes are routed deterministically:

- 2.02 → earnings;
- 3.02 and registration/prospectus forms → financing/dilution;
- 2.01 → merger/acquisition;
- 5.02 → management change;
- 1.01 → contract/partnership;
- 1.03, 2.04, 2.05, 4.01, or 4.02 → distress/restatement risk.

Routine 424B2 structured-note filings are intentionally outside the relevant form set.

## Model boundary

The deterministic layer assigns evidence categories only. It does not claim that a
catalyst is positive, nor does it create an intraday continuation probability. Both
`model_score` and `model_provenance` are null for all 97 candidate symbols. The quality
gate fails if an uncalibrated score is inserted.

The next stage is Beijing 20:00 verification of this locked symbol set using premarket
same-time RVOL, followed by point-in-time market-cap, earnings-day, and LULD checks.

## Operational evidence

A fresh run took about 291 seconds because SEC issuer submissions were fetched for
exact timestamps. Raw provider snapshots are immutable and reusable; a deterministic
rebuild after cleaning-rule changes took under seven seconds and made no network calls.

Provider semantics were verified against official documentation:

- <https://docs.alpaca.markets/us/reference/news-3>
- <https://massive.com/blog/new-and-improved-financial-news-api>
- <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
- <https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits>
