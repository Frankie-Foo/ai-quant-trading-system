CREATE SCHEMA IF NOT EXISTS quant_agent;

CREATE TABLE IF NOT EXISTS quant_agent.tool_audit (
    audit_id text PRIMARY KEY,
    actor text,
    tool text NOT NULL,
    request_json jsonb NOT NULL,
    response_json jsonb,
    success boolean NOT NULL,
    error_code text,
    created_at_utc timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS quant_agent.agent_theses (
    record_id text PRIMARY KEY,
    actor text NOT NULL,
    trade_date date NOT NULL,
    category text,
    status text NOT NULL CHECK (status = 'shadow'),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    document_json jsonb NOT NULL,
    created_at_utc timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS quant_agent.lessons (
    record_id text PRIMARY KEY,
    actor text NOT NULL CHECK (actor = 'pdca'),
    trade_date date NOT NULL,
    category text NOT NULL CHECK (
        category IN ('selection_review', 'signal_decay', 'execution_gap', 'cost_drift')
    ),
    status text NOT NULL CHECK (status = 'accepted_fact'),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    document_json jsonb NOT NULL,
    created_at_utc timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS quant_agent.audit_reports (
    record_id text PRIMARY KEY,
    actor text NOT NULL CHECK (actor = 'discipline'),
    trade_date date NOT NULL,
    category text,
    status text NOT NULL CHECK (status IN ('complete', 'incomplete_evidence')),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    document_json jsonb NOT NULL,
    created_at_utc timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS quant_agent.evolution_proposals (
    record_id text PRIMARY KEY,
    actor text NOT NULL CHECK (actor = 'pdca'),
    trade_date date NOT NULL,
    category text,
    status text NOT NULL CHECK (status = 'draft'),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    document_json jsonb NOT NULL CHECK (
        document_json ->> 'status' = 'draft'
        AND (document_json ->> 'production_eligible')::boolean IS FALSE
    ),
    created_at_utc timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS quant_agent.agent_tradeplan_drafts (
    record_id text PRIMARY KEY,
    actor text NOT NULL CHECK (actor = 'commander'),
    trade_date date NOT NULL,
    category text,
    status text NOT NULL CHECK (status = 'shadow_draft'),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    document_json jsonb NOT NULL CHECK (
        document_json ->> 'status' = 'shadow_draft'
        AND (document_json ->> 'execution_eligible')::boolean IS FALSE
        AND (document_json ->> 'broker_submission_count')::integer = 0
    ),
    created_at_utc timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS agent_theses_date_actor_idx
    ON quant_agent.agent_theses (trade_date, actor);
CREATE INDEX IF NOT EXISTS lessons_date_category_idx
    ON quant_agent.lessons (trade_date, category);
CREATE INDEX IF NOT EXISTS audit_reports_date_idx
    ON quant_agent.audit_reports (trade_date);
CREATE INDEX IF NOT EXISTS evolution_proposals_date_idx
    ON quant_agent.evolution_proposals (trade_date);
CREATE INDEX IF NOT EXISTS tool_audit_created_idx
    ON quant_agent.tool_audit (created_at_utc DESC);
