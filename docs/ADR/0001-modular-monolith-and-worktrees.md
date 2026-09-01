# ADR-0001: Modular monolith with task-isolated worktrees

- Status: accepted
- Date: 2026-08-07

## Context

The repository contains deterministic strategy policy, data adapters, research
jobs, Paper execution, agent tooling, an Electron client, scheduled tasks, and
Feishu audit projection. The current code already has meaningful boundaries, but
release governance, migration ownership, CI coverage, and task isolation are
incomplete. A big-bang package move or microservice split would increase risk to
the frozen trading interfaces.

## Decision

Keep one repository and one modular monolith. `main` is the production baseline.
Each requirement uses one branch and one Git worktree. Business policy stays in
stable inner modules; networks, databases, vendors, UI, schedulers, and LLMs are
outer adapters. Scripts remain compatibility entrypoints while their business
logic is extracted incrementally behind use-case interfaces.

Every parallel change must have a distinct worktree. Worktrees are created from
the latest `main`, reviewed through pull requests, merged, and removed after
merge. Production deploys a recorded `main` commit only.

## Consequences

Positive:

- parallel tasks cannot overwrite each other's files or branch state;
- kernel policy remains testable without network, database, or LLM dependencies;
- vendor and delivery changes remain replaceable at explicit seams;
- rollback and audit evidence have a single release source.

Costs:

- contributors must manage worktree lifecycle and keep branches current;
- some legacy scripts remain during incremental extraction;
- GitHub remote configuration and required reviewers are external setup work.

## Rejected alternatives

- Direct development on `main`: no review or rollback boundary.
- One shared long-lived worktree: parallel tasks overwrite state.
- Immediate microservices: adds deployment and failure domains without measured
  scale or ownership need.
- A generic `common/` package: hides ownership and creates inward dependency leaks.

