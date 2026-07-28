# Intraday selection postmortem record contract

## Required identity and audit fields

- `session_date`
- `selection_cutoff_utc`
- `opportunity_rank`
- `case_id` after gateway anonymization
- `schema_version`
- `provenance`
- source snapshot IDs from the tool envelope

## Outcome fields

| `decision_outcome` | Meaning | Research handling |
|---|---|---|
| `captured_opportunity` | Passed the frozen selection gates | Compare thesis support separately from profit |
| `intentional_rejection` | Was visible but failed a recorded gate | Counterfactual research only; never relax a hard gate automatically |
| `missed_detectable_opportunity` | Evidence existed by the cutoff or a factor family was absent | Eligible for repeated-pattern analysis |
| `unpredictable_after_cutoff` | Material catalyst first appeared after the selection cutoff | Record as unavoidable; do not train it as a miss |
| `incomplete_evidence` | A required evidence source was unavailable | Restore evidence before attribution |

## Root causes

- `selected`
- `intentional_gate`
- `late_catalyst`
- `data_or_classifier_gap`
- `factor_gap`
- `incomplete_evidence`

Do not invent new root-cause strings in narrative output. Propose taxonomy changes only as draft
research work.

## Outcome measurements

The deterministic record may include close return, MFE from the prior close, MAE from the prior
close, liquidity, and selection rank. Treat every number as valid only through its matching `Fact`
and provenance. These measurements describe the observed path; they do not prove causal skill,
tradability, or achievable execution.

## Pattern safety

- `pattern_key` must not contain a ticker.
- `research_eligible=true` permits clustering, not production mutation.
- `production_change_allowed` must always be false.
- A proposed factor change must pass the existing minimum sample, net-cost, purged OOS,
  champion/challenger, and human-approval gates.
