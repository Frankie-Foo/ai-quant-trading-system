# Trading system engineering rules

1. `kernel/` is the deterministic fast loop. It must not import or call any LLM SDK.
2. Every decision-time number must carry provenance; unavailable data must degrade to
   `N/A`, never an estimate presented as fact.
3. Hard arbitration order is defined only in `kernel/guardrails.py`: P0, then P1,
   then P2.
4. The system is permanently long-only. No module may add a short execution branch.
5. All stored timestamps are timezone-aware UTC. Market logic uses
   `America/New_York`; lock logic uses `Asia/Shanghai`.
6. Rolling features must be shifted so that only information known at `asof` is used.
7. Synthetic data is allowed only in unit tests. It must never be presented as
   strategy performance evidence.
8. Cost assumptions may only become more conservative without explicit owner approval.
9. Each milestone is test-first and updates `PROGRESS.md` with exact evidence.
10. A dataset with a failed critical quality check is quarantined and must not feed
    features, backtests, models, or orders.
11. Missing or halted intraday bars are never forward-filled or interpolated.
12. Offline and online feature values must come from the same point-in-time function.
13. Research runs record data, feature, config, and code hashes plus the number of
    attempted configurations before model comparison.
14. Trading-plane order state transitions are explicit and idempotent; process
    recovery must never create a duplicate broker order.
