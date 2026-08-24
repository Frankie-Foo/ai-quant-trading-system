# AI4S research loop

The trading system applies AI4S as a governed research method, not as autonomous
production trading. The deterministic kernel, long-only constraint, risk controls,
cost assumptions, and human production approval remain unchanged.

## Scientific cycle

1. **Observe:** combine objective outcomes, execution evidence, explicit corrections,
   and implicit adoption signals. Objective market and broker evidence has the highest
   weight; complete human corrections rank next; implicit behavior is diagnostic only.
2. **Hypothesize:** express one causal mechanism, one changed variable, a control, and
   an explicit falsification test. A loss alone does not refute a mechanism, and a gain
   alone does not prove one.
3. **Experiment:** freeze point-in-time inputs, configuration, code, costs, chronological
   train/validation/blind windows, negative control, and attempted configuration count.
4. **Critique:** deterministic rules check data quality, leakage, costs, sample size, and
   blind-test use. The Critic Agent may reject or request evidence but cannot promote.
5. **Learn:** store accepted, rejected, and inconclusive conclusions with provenance in
   anonymous Memory. Store causal conclusions and failure regimes, not parameter dumps.
6. **Advance safely:** a passing backtest may enter Alpaca Paper only. At least thirty
   independent Paper trading days are required before human review. No AI decision can
   become production eligible or weaken risk controls.

## Deterministic admission gate

`research.registry.evaluate_experiment` rejects evidence unless it uses point-in-time
data, complete quote-aware costs, passed critical quality checks, and exactly one blind
evaluation. It then requires adequate full and blind samples, positive net expectancy,
profit factor of at least one, blind average win/loss of at least 1.2, and a positive
lower confidence bound. Passing historical evidence reaches only `eligible_for_paper`.

After thirty independent Paper days, evidence reaches only
`eligible_for_human_review`; `production_eligible` remains false.

## Evolution modes

- Feedback alignment diagnoses selection, sizing, slippage, and execution gaps.
- Retrieval evolution refreshes catalyst and market-regime knowledge without changing
  trading code.
- Agent self-correction proposes and criticizes falsifiable experiments.
- Modular growth keeps trend, reversal, volatility, catalyst, and risk hypotheses
  independently testable and removable.

Memory replay retains prior accepted and rejected evidence to prevent repeated failed
experiments and capability drift. New experiments run in sandbox, then Paper. Rollback
means selecting the last approved immutable strategy version; automatic production
mutation is prohibited.

## Current finding and next experiment

The three-year Modern H15 breakout plus one re-entry experiment is falsified: historical
and blind net expectancy are negative after costs. The next isolated experiment changes
only `entry_mode`: remove the first straight breakout and require a completed pullback,
H15/VWAP support, rising VWAP, and renewed higher-price acceptance. The existing
breakout strategy remains the control. Success requires the same frozen data, costs,
time split, and admission gate.
