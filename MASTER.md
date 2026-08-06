# Trading System v2

This repository implements the frozen specification supplied by Frank in
`C:/Users/frank/Desktop/trading-system-v2-master.md` on 2026-07-16.

Frozen source SHA-256:
`e6387614f5b7dd3f5c9d4a1420eec415494152e34fe76209ecc7f6057b0dd6bc`.

The authoritative specification remains that file while implementation proceeds.
Frozen public interfaces, configuration structure, invariants, milestone acceptance
criteria, and database schema must not be changed by the implementer.

Core invariants:

- deterministic fast loop with zero LLM calls;
- provenance for every decision-time number;
- one arbitration order: P0 -> P1 -> P2;
- permanent long-only execution;
- point-in-time data and next-bar execution;
- conservative, explicit transaction costs.
