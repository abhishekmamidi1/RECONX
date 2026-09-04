-- 0003: add 'batch' to matches.match_type
-- The many-to-one aggregated-payout grouping stage persists its outcomes as
-- match_type='batch' inside the existing matches / match_participants model.
-- The live constraint was auto-named by init.sql (matches_match_type_check);
-- the SQLAlchemy model names it ck_matches_type. Drop whichever exists.

DO $$
BEGIN
    ALTER TABLE matches DROP CONSTRAINT IF EXISTS matches_match_type_check;
    ALTER TABLE matches DROP CONSTRAINT IF EXISTS ck_matches_type;
END $$;

ALTER TABLE matches
    ADD CONSTRAINT ck_matches_type
    CHECK (match_type IN ('deterministic', 'fuzzy', 'semantic', 'ai', 'manual', 'batch'));