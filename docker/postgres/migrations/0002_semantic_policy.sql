-- 0002: semantic matching + AI reasoning policy seeds (Phase 3)

INSERT INTO policy_config (key, value, description, updated_by) VALUES
    ('matching.semantic.enabled',
     'true'::jsonb,
     'Master switch for embedding-based candidate retrieval over unmatched records',
     'system'),
    ('matching.semantic.similarity_threshold',
     '0.82'::jsonb,
     'Minimum cosine similarity for a retrieved neighbour to become an AI-evaluation candidate',
     'system'),
    ('matching.semantic.top_k',
     '5'::jsonb,
     'Number of nearest neighbours surfaced per unmatched record',
     'system'),
    ('gate.ai_reasoning_enabled',
     'true'::jsonb,
     'Master switch for the AI reasoning agent over semantic candidates',
     'system')
ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value,
        description = EXCLUDED.description,
        updated_at = now();
