-- 0003: joint-evidence similarity floor for AI auto-resolve.
--
-- Fixes small-model overconfidence: a 1:1 semantic 'match' may auto-resolve
-- only when BOTH conditions hold (similarity >= this floor AND confidence >=
-- gate.ai_min_confidence_autoresolve). A weak-similarity pair (e.g. the
-- golden case pay_Pl4tFrmZ / INV-2026-0721 at bge-m3 similarity 0.571) is
-- forced to needs_human regardless of the model's claimed confidence.
--
-- Calibrated for real BGE embeddings: pay_Sm1Th1cA/INV-2026-0720 (genuine
-- match) cosine ~0.66 passes; pay_Pl4tFrmZ/INV-2026-0721 (ambiguous, must go
-- to human) cosine ~0.57 is blocked. The hermetic test suite overrides this
-- key per-run because the hashing test double produces lower similarities.

INSERT INTO policy_config (key, value, description, updated_by) VALUES
    ('matching.ai.similarity_autoresolve_min',
     '0.60'::jsonb,
     'Minimum cosine similarity for an AI match to auto-resolve (joint-evidence gate with confidence; below this, even a confident match falls to human review)',
     'system')
ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value,
        description = EXCLUDED.description,
        updated_at = now();