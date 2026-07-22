# Postmarket agent boundary

M8 was deliberately redesigned from seven unspecified Accio identities to two narrow,
read-only slow-loop roles:

- the Research Agent may summarize a frozen Trading Episode and propose falsifiable
  sandbox hypotheses;
- the Critic Agent receives the same fact package plus the structured proposal and
  attempts to reject it for leakage, overfitting, unsupported causality, missing costs,
  or insufficient evidence.

The executable contracts and versioned prompts live in
`research/postmortem_agents.py`. Data, labels, scheduling, experiment admission,
production promotion, risk, and execution remain deterministic Python responsibilities.
Neither agent can edit strategy code, approve a model for production, or place orders.

The production default is hybrid. `research/program_review.py` first calculates the
facts, rejects missing paths or costs, enforces sample thresholds, and creates only
allowlisted sandbox specifications. Research and Critic run only after those gates pass.
They receive separate prompt contexts over the same no-lookahead fact package. An API
failure is recorded without converting unavailable analysis into approval.
