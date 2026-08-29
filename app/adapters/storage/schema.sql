-- PostgreSQL schema for OverFast API persistent storage
--
-- CREATE TABLE IF NOT EXISTS with no migration tool is a deliberate choice, not
-- an oversight. Both tables hold nothing but regenerable Blizzard content:
-- static_data is scraped HTML and player_profiles is cached profiles, so the
-- migration for any breaking change is DROP TABLE plus a refetch. That costs
-- Blizzard round-trips behind the throttle and nothing else — there is no
-- user-owned data here to migrate, and no state that cannot be rebuilt.
--
-- Adding Alembic would buy a guarantee this schema does not need and leave a
-- versions/ directory to keep honest forever. Revisit only if a table ever
-- starts holding something Blizzard cannot hand back.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'static_data_category') THEN
        CREATE TYPE static_data_category AS ENUM ('heroes', 'hero', 'gamemodes', 'maps', 'roles');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS static_data (
    key          VARCHAR(255)           PRIMARY KEY,
    data         BYTEA                  NOT NULL,
    category     static_data_category   NOT NULL,
    data_version SMALLINT               NOT NULL DEFAULT 1,
    created_at   TIMESTAMPTZ            NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ            NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS player_profiles (
    player_id               TEXT        PRIMARY KEY,
    battletag               TEXT,
    name                    TEXT,
    html_compressed         BYTEA       NOT NULL,
    summary                 JSONB,
    last_updated_blizzard   BIGINT,
    data_version            SMALLINT    NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_player_profiles_updated_at
    ON player_profiles (updated_at);

CREATE INDEX IF NOT EXISTS idx_player_profiles_battletag
    ON player_profiles (battletag)
    WHERE battletag IS NOT NULL;
