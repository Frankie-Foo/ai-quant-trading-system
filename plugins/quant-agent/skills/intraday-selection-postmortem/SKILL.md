---
name: intraday-selection-postmortem
description: Audit a completed U.S. equity session against the frozen premarket selection, explain captured and missed liquid intraday opportunities, group repeated ticker-anonymous failure patterns, and draft falsifiable sandbox research hypotheses. Use for daily post-close stock-selection review, missed-mover analysis, catalyst coverage gaps, factor or order-flow coverage gaps, and questions about why selected stocks underperformed or unselected stocks rallied.
---

# Intraday Selection Postmortem

Review selection quality after the XNYS close. Treat the accepted deterministic snapshot as the
fact record. Never reconstruct facts from charts, current web pages, or hindsight.

## Invariants

- Query only accepted `intraday_selection_postmortems`, `trading_episodes`, and supporting
  snapshots through the allowlisted gateway.
- Require `intraday_selection_postmortem.v1`; stop with `incomplete_evidence` for an unavailable,
  quarantined, mixed-session, or incompatible snapshot.
- Preserve `N/A`. Never reinterpret missing news as a factor gap.
- Keep research patterns ticker-anonymous. Use `case_id` and `pattern_key`; symbols may appear only
  in an operator-facing audit requested by the owner.
- Separate selection quality from execution quality and realized profit.
- Never submit orders, alter weights, relax hard gates, edit production configuration, or mark a
  proposal production-eligible.

Read [references/record-contract.md](references/record-contract.md) when validating fields,
outcomes, or root causes.

## Workflow

1. Resolve the requested completed session, or the latest available completed session.
2. Query `intraday_selection_postmortems` for the session. Verify the snapshot ID, schema,
   selection cutoff, `production_change_allowed=false`, unique cases, and complete provenance.
3. Partition the records by `decision_outcome`:
   - captured opportunity;
   - intentional rejection;
   - missed but detectable opportunity;
   - unpredictable after-cutoff catalyst;
   - incomplete evidence.
4. Report coverage and root-cause counts using only supplied `Fact` references. Do not treat a
   high close return as proof that the stock was safely tradable at the decision cutoff.
5. Query prior accepted postmortems and group only by ticker-free `pattern_key`. A single event is
   an observation, not a lesson. Keep a pattern in observation status until it reaches the
   system-configured cluster threshold.
6. For repeated eligible patterns, create a falsifiable research hypothesis:
   - `intentional_gate`: test a counterfactual while keeping the hard guardrail unchanged;
   - `data_or_classifier_gap`: audit ingestion, entity mapping, timestamp, and classifier recall;
   - `factor_gap`: test price, order-flow, sector, or market-regime features in the sandbox.
7. Define the rejection test before evaluating the hypothesis. Require chronological or purged
   OOS validation, net costs, attempted-configuration count, and unchanged hard risk gates.
8. Return no hypothesis for `late_catalyst`, `incomplete_evidence`, or a one-off pattern.

## Output

Return a compact structured review containing:

- session and source snapshot IDs;
- captured, intentionally rejected, missed-detectable, unpredictable, and incomplete counts;
- repeated pattern clusters with observation count and distinct-session count;
- zero or more hypothesis cards containing mechanism, evidence references, falsification test,
  target component, sandbox-only action, and missing evidence;
- an explicit statement that production changes and orders equal zero.

An empty hypothesis list is a valid and preferred result when evidence is insufficient.
