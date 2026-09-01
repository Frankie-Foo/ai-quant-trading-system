# AI Quant Paper runbook

## Normal flow

- 08:00 ET: build at most ten names, market cap at least $1 billion; write the dedicated
  Investment Base and send one Chinese Livermore summary. No orders.
- 09:25 ET: read only the frozen first pool, verify SIP price/volume, VWAP, spread and
  liquidity, retain zero to six, persist and notify. A price below premarket VWAP or a
  0.30%-1.00% premarket spread is a yellow observation flag, not an automatic rejection.
  Missing SIP facts, dollar volume below $1 million, or spread above 1.00% still rejects.
  No orders.
- 09:35 ET: read only the second pool and five completed opening minutes, retain zero to
  three, publish the complete plan, then create immutable authorization. Zero candidates
  is a valid no-trade result and must include rejection reasons.
- 09:56–15:00 ET: the Modern H15 monitor may place protected Alpaca Paper orders. It
  polls locally at one-second cadence but records only state transitions.
- 15:45 ET: cancel unfilled entry orders. 15:50 ET: flatten all system-owned positions.

Every entry is a marketable limit bracket with an atomic stop and 3R target. Actual SIP
spread and signal-to-ask slippage must each fit within 0.25%; total stop including reserve is at
most 2%. Risk limits are 0.5% per symbol, 0.75% per sector and 1.5% portfolio. At a 1.5%
daily loss no new entry is allowed; at 2% the runtime flattens and freezes. Attempts use
60% then 40% of the symbol budget, at most twice.

## Alerts and recovery

The first runtime fault sends one Chinese alert and blocks new entries. The third
consecutive failure sends one escalation and latches the freeze. Recovery sends one
message but never removes the freeze automatically. Existing positions continue through
protective exits. Feishu failure must not prevent a broker exit or its Livermore fill
notification.

On restart, reconcile SQLite intents, client order IDs, broker orders and positions.
Unknown orders, unknown positions, ambiguous notification delivery or a second active
lease freezes the runtime. Do not delete `runs/` to make a mismatch disappear.

## Emergency procedure

1. Set `TRADING_KILL_SWITCH=true` and disable `Trading System V2 - AI Quant Funnel`.
2. Inspect Alpaca Paper positions and open orders. Stopping the process does not cancel
   orders.
3. Preserve logs, SQLite ledgers and `open_confirmation.json`.
4. Correct the fault on a branch, run all release gates, and reconcile again.
5. Re-enable only after owner confirmation. Never switch the host to a live endpoint.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy data_plane kernel research execution schedule `
  agent_gateway operations scripts tests
.\.venv\Scripts\python.exe -m scripts.run_paper_acceptance_drills
```

External checks are read-only. Validate Alpaca account identity, dedicated Base token
fingerprint/table IDs and Livermore bot/channel. Never open a disconnected old Base.

## Governed strategy loop

The daily funnel evaluates the active policy and an optional shadow challenger against
the same frozen first-wave pool. Shadow candidates always have
`execution_eligible=false`. Postmarket writes one accepted shadow outcome per session.
The monthly task may turn a Memory-backed, OOS-accepted RVOL proposal into a challenger;
it cannot change the active policy.

Use `scripts.manage_strategy_policy approve` only after reviewing its 20-session gate
and pass the exact reviewed Challenger hash with `--confirm-policy-hash`. It requires a
named human approver and atomically archives the old policy. `rollback`
accepts only a hash-verified archived version. Neither command enables live trading.
