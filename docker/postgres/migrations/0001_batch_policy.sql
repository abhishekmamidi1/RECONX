-- 0001: many-to-one batch grouping policy seeds (Phase 2 decision lock-in)
-- Pairwise deterministic/fuzzy stages stay strictly 1:1; aggregated payouts
-- are handled by an exact subset-sum grouping stage configured here.

INSERT INTO policy_config (key, value, description, updated_by) VALUES
    ('matching.batch.enabled',
     'true'::jsonb,
     'Many-to-one aggregated-payout grouping stage (exact subset-sum over same-window credits). Pairwise deterministic/fuzzy stages remain strictly 1:1',
     'system'),
    ('matching.batch.max_components',
     '10'::jsonb,
     'Maximum number of gateway/ERP credits allowed to aggregate into one bank credit',
     'system'),
    ('matching.batch.date_window_days',
     '3'::jsonb,
     'Every batch component must fall within this window of the bank credit business_date',
     'system'),
    ('matching.batch.amount_tolerance_pct',
     '0'::jsonb,
     'Fractional tolerance between summed components and the bank credit amount (0 = exact)',
     'system')
ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value,
        description = EXCLUDED.description,
        updated_at = now();
