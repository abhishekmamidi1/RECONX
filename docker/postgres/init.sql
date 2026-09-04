-- =============================================================
-- Payment Reconciliation System — Phase 1 schema
-- Executed once on first container init (empty pgdata volume).
-- Source of truth until Alembic migrations are introduced.
-- Money: NUMERIC(18,2), major units (INR). Amounts are signed-magnitude
-- convention: stored *positive* (magnitude) with the direction column carrying
-- credit/debit semantics and transaction_type carrying settlement/refund
-- classification, so refunds never flow through positive-credit math as negatives.
-- Every table that records a decision keeps full provenance so
-- nothing downstream is a black box.
-- =============================================================

CREATE TABLE ingestions (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source                 TEXT NOT NULL CHECK (source IN ('razorpay', 'bank', 'erp')),
    filename               TEXT NOT NULL,
    checksum_sha256        CHAR(64),
    rows_total             INTEGER NOT NULL DEFAULT 0,
    rows_inserted          INTEGER NOT NULL DEFAULT 0,
    rows_skipped_duplicate INTEGER NOT NULL DEFAULT 0,
    rows_rejected          INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'completed', 'failed')),
    error_detail           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ingestions_source_created ON ingestions (source, created_at DESC);

CREATE TABLE transactions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_id     UUID REFERENCES ingestions(id) ON DELETE SET NULL,
    source           TEXT NOT NULL CHECK (source IN ('razorpay', 'bank', 'erp')),
    external_ref     TEXT NOT NULL,
    amount           NUMERIC(18,2) NOT NULL CHECK (amount >= 0),
    direction        TEXT NOT NULL DEFAULT 'credit' CHECK (direction IN ('credit', 'debit')),
    transaction_type TEXT NOT NULL DEFAULT 'settlement'
                     CHECK (transaction_type IN ('settlement', 'refund')),
    currency         CHAR(3) NOT NULL DEFAULT 'INR',
    txn_date         TIMESTAMPTZ NOT NULL,
    narration        TEXT,
    counterparty     TEXT,
    status           TEXT,
    raw              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_transactions_source_external_ref UNIQUE (source, external_ref)
);

CREATE INDEX idx_transactions_source_date   ON transactions (source, txn_date);
CREATE INDEX idx_transactions_source_amount ON transactions (source, amount);

CREATE TABLE matches (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    match_type       TEXT NOT NULL
                     CHECK (match_type IN ('deterministic', 'fuzzy', 'semantic', 'ai', 'manual', 'batch')),
    confidence_score NUMERIC(5,4) NOT NULL CHECK (confidence_score >= 0 AND confidence_score <= 1),
    status           TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    resolved_by      TEXT CHECK (resolved_by IN ('auto', 'human')),
    decided_by       TEXT,
    rationale        TEXT,
    policy_snapshot  JSONB,
    proposed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ
);

CREATE INDEX idx_matches_status_proposed ON matches (status, proposed_at DESC);
CREATE INDEX idx_matches_type_confidence ON matches (match_type, confidence_score DESC);

CREATE TABLE match_participants (
    match_id       UUID NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    transaction_id UUID NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    role           TEXT NOT NULL DEFAULT 'participant'
                   CHECK (role IN ('primary', 'candidate', 'participant')),
    PRIMARY KEY (match_id, transaction_id)
);

CREATE INDEX idx_match_participants_txn ON match_participants (transaction_id);

CREATE TABLE exceptions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID REFERENCES transactions(id) ON DELETE SET NULL,
    exception_type  TEXT NOT NULL
                    CHECK (exception_type IN (
                        'unmatched',
                        'amount_mismatch',
                        'duplicate_suspect',
                        'low_confidence_ai',
                        'manual_review_required',
                        'refund'
                    )),
    priority        TEXT NOT NULL DEFAULT 'medium'
                    CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    amount_impact   NUMERIC(18,2),
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'in_review', 'escalated', 'resolved', 'dismissed')),
    assigned_to     TEXT,
    resolution_note TEXT,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX idx_exceptions_status_opened ON exceptions (status, opened_at);
CREATE INDEX idx_exceptions_priority      ON exceptions (priority, opened_at DESC);

CREATE TABLE audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    actor        TEXT NOT NULL DEFAULT 'system',
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    UUID,
    before_state JSONB,
    after_state  JSONB,
    details      JSONB,
    request_id   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_entity ON audit_logs (entity_type, entity_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_logs (action, created_at DESC);

CREATE TABLE policy_config (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT 'system',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO policy_config (key, value, description) VALUES
    ('matching.deterministic.enabled',         'true'::jsonb,     'Master switch for the deterministic matcher stage'),
    ('matching.deterministic.date_window_days','0'::jsonb,        'Allowed day delta between dates for an exact match'),
    ('matching.fuzzy.enabled',                 'true'::jsonb,     'Master switch for the fuzzy matcher stage'),
    ('matching.fuzzy.score_threshold',         '85'::jsonb,       'RapidFuzz score (0-100) above which a fuzzy candidate is accepted as a proposal'),
    ('matching.fuzzy.amount_tolerance_pct',    '0.001'::jsonb,     'Fractional tolerance on amount (0.001 = 0.1%) for fuzzy matching'),
    ('matching.fuzzy.date_window_days',        '5'::jsonb,        'Date window +/- days within which fuzzy candidates may be considered'),
    ('matching.semantic.enabled',              'true'::jsonb,     'Master switch for the semantic matcher stage'),
    ('matching.semantic.similarity_threshold', '0.82'::jsonb,     'Cosine similarity threshold for BGE embedding candidates'),
    ('matching.semantic.top_k',                '5'::jsonb,        'Number of nearest neighbours retrieved per unmatched record'),
    ('gate.ai_min_confidence_autoresolve',     '0.90'::jsonb,     'Minimum AI-agent confidence for auto-resolve eligibility'),
    ('matching.ai.similarity_autoresolve_min', '0.60'::jsonb,     'Minimum cosine similarity for an AI match to auto-resolve (joint-evidence gate with confidence; below this, even a confident match falls to human review)'),
    ('gate.auto_resolve_enabled',              'true'::jsonb,     'Global auto-resolve master switch (confidence alone is never sufficient)'),
    ('materiality.max_abs_discrepancy_inr',    '500.00'::jsonb,   'Auto-resolve forbidden when absolute discrepancy exceeds this INR value'),
    ('materiality.max_discrepancy_pct',        '0.02'::jsonb,     'Auto-resolve forbidden when discrepancy exceeds this fraction of transaction value'),
    ('review.force_human_above_inr',           '100000.00'::jsonb,'Exceptions at or above this amount always route to human review regardless of confidence'),
    ('matching.batch.enabled',                 'true'::jsonb,     'Many-to-one aggregated-payout grouping stage (exact subset-sum over same-window credits). Pairwise deterministic/fuzzy stages remain strictly 1:1'),
    ('matching.batch.max_components',          '10'::jsonb,       'Maximum number of gateway/ERP credits allowed to aggregate into one bank credit'),
    ('matching.batch.date_window_days',        '3'::jsonb,        'Every batch component must fall within this window of the bank credit business_date'),
    ('matching.batch.amount_tolerance_pct',    '0'::jsonb,        'Fractional tolerance between summed components and the bank credit amount (0 = exact)')
;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_exceptions_updated_at
    BEFORE UPDATE ON exceptions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_policy_config_updated_at
    BEFORE UPDATE ON policy_config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
