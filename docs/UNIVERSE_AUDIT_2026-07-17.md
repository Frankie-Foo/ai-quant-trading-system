# Daily universe audit — 2026-07-17

## Outcome

The first real, point-in-time daily pre-universe snapshot was accepted:

- dataset: `kernel.universe.daily_precheck-20260717T161535Z-5dd1b9e8f1ac`
- target session: 2026-07-17
- information cutoff: 2026-07-16
- active Massive common stocks observed on the cutoff session: 5,179
- passed price/elasticity/identity precheck: 2,871
- rejected probable ticker-identity discontinuities: 377
- final `pass_gate=true`: 0
- critical quality failures: 0

This is a candidate pre-pool, not a stock recommendation or performance result.
The final gate remains closed until RVOL, point-in-time market capitalisation,
earnings exclusion, and LULD state are supplied.

## Inputs and lineage

The snapshot has 301 immutable parents: the last 300 accepted Massive grouped-daily
sessions and the accepted 2026-07-16 active common-stock reference snapshot.  All
decision-time price, ADV, Beta, ATR, security-type, and identity-check fields carry
explicit provenance columns.

The daily feature builder filters `trade_date < target_date` before calculating any
rolling value. A unit test injects an extreme target-date bar and confirms that it
cannot change the selected price or ADV.

## Defects found by the real-data audit

The first unfiltered run mixed common stocks with ETFs, leveraged products, and other
listed instruments. Massive `type=CS` reference data reduced the target population
from 12,454 traded symbols to 5,179 active common stocks.

The second run exposed ticker reuse. For example, the current NINE security has a
2026-03-31 list date while grouped history also contains an older NINE identity;
TDTH has a 2026-07-16 list date. Concatenating those identities produced false Beta
and ATR values. The kernel now fails closed when a lookback contains an absolute
one-day return above 90%, marking `suspected_identity_discontinuity`. This conservative
rule also rejects genuine extreme jumps, which is intentional until detailed FIGI and
listing-date verification is performed downstream.

## Remaining data gates

1. RVOL must come from the same point-in-time intraday function in research and live
   operation; it cannot be inferred from tomorrow's full-day volume.
2. The batch ticker directory identifies common stock but does not include market cap.
   Massive's single-ticker overview exposes market cap and list date, so the economical
   design is to call it only for the small set surviving the RVOL/catalyst stage.
3. Earnings-calendar and LULD inputs still need authoritative point-in-time sources.
4. Catalyst evidence and ranking are not yet connected. No language model is allowed
   to fabricate a missing catalyst or override these deterministic gates.

## Verification

- feature/universe tests cover ATR, Beta, ADV, target-date no-lookahead, common-stock
  filtering, missing-data fail-closed behavior, and ticker-identity discontinuity.
- the persisted snapshot passed non-empty, unique-symbol, point-in-time cutoff,
  common-stock-only, valid-price, fail-closed, and provenance checks.

Massive documents that grouped daily aggregates are available in all Stocks plans,
that split events support historical adjustment factors, and that ticker events are
point-in-time rather than automatically concatenated. Those semantics informed the
identity gate:

- <https://massive.com/docs/rest/stocks>
- <https://massive.com/docs/rest/stocks/corporate-actions/splits>
- <https://massive.com/knowledge-base/article/how-does-polygon-handle-ticker-changes-and-acquisitions>
