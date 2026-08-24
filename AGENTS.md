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

## Intraday monitoring operating order

For every user-supplied daily intraday plan, use this fixed sequence:

1. Read the whole plan, extract symbols, roles, thresholds, risk/capital caps,
   ETF or sector gates, and time rules. Reconcile missing decision fields from
   traceable current SIP data where possible; otherwise record `N/A` and block
   the dependent condition. Never invent a value.
2. Configure or update the read-only 1-second monitor and send one initial
   Chinese Buffett-bot plan summary before entries begin: pool, buy gates,
   ETF anchors, positions reported by the user, and explicit no-buy boundaries.
3. During the valid ET session, send only immediate action events plus a
   half-hour ranked status summary: 可买, 强观察, 继续观察, 不能买. Include an
   explicit reason for every symbol. Do not send routine per-second updates.
4. Before every Chinese group message, perform a UTF-8 round-trip check and
   reject literal `?` and U+FFFD. Record result, message ID, and a time-bucket
   dedupe key. Use only the configured Buffett bot/channel. Never call real or
   Paper order APIs.
