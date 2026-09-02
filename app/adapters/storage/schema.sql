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
        CREATE TYPE static_data_category AS ENUM ('heroes', 'hero', 'gamemodes', 'maps', 'roles', 'patch_notes');
    END IF;
END $$;

-- The block above only runs when the type is ABSENT, so on any database that
-- already has it a value added to StaticDataCategory would never reach postgres
-- and every write in that category would fail. These ALTERs are the migration:
-- idempotent, safe on every boot, and no-ops on a database that just ran the
-- CREATE TYPE above. One line per StaticDataCategory member, and
-- tests/adapters/storage/test_schema.py fails if a member has no line here.
-- (PostgreSQL 12+ allows ADD VALUE inside a transaction block as long as the
-- new value is not used in the same transaction; nothing below uses one.)
ALTER TYPE static_data_category ADD VALUE IF NOT EXISTS 'heroes';
ALTER TYPE static_data_category ADD VALUE IF NOT EXISTS 'hero';
ALTER TYPE static_data_category ADD VALUE IF NOT EXISTS 'gamemodes';
ALTER TYPE static_data_category ADD VALUE IF NOT EXISTS 'maps';
ALTER TYPE static_data_category ADD VALUE IF NOT EXISTS 'roles';
ALTER TYPE static_data_category ADD VALUE IF NOT EXISTS 'patch_notes';

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

-- Snapshot history: one small row per profile version we ever served.
--
-- This is the one table whose contents Blizzard cannot hand back. Blizzard
-- publishes no rank history, no match history and no session data, so a row
-- deleted here is gone for good — but each row is derived from a scrape we were
-- doing anyway, so the table costs no extra Blizzard traffic.
--
-- The primary key is what makes the write idempotent: (player_id,
-- last_updated_blizzard) identifies a profile *version*, so serving the same
-- version again inserts nothing (ON CONFLICT DO NOTHING) instead of duplicating
-- a row on every request.
--
-- Deliberately NO foreign key to player_profiles. The daily cleanup_stale_players
-- job deletes the profiles of players who stopped being requested, and a
-- REFERENCES clause would take their history with them — exactly the data that
-- cannot be refetched. Snapshots age out on their own clock
-- (player_snapshot_max_age), not on the profile's.
CREATE TABLE IF NOT EXISTS player_snapshots (
    player_id             TEXT        NOT NULL,
    last_updated_blizzard BIGINT      NOT NULL,
    taken_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data                  JSONB       NOT NULL,
    PRIMARY KEY (player_id, last_updated_blizzard)
);

CREATE INDEX IF NOT EXISTS idx_player_snapshots_player_taken
    ON player_snapshots (player_id, taken_at DESC);

-- Hero stats history: one row per day per recorded filter combination.
--
-- Like player_snapshots, this is data Blizzard cannot hand back — the rates
-- page reports the current moment and nothing else, so a deleted row is gone.
-- Unlike player_snapshots it is not a by-product of serving a request: a daily
-- cron pays for it, which is why only ONE slice of the /heroes/stats cross
-- product is recorded (see HERO_STATS_SNAPSHOT_SLICES in the hero service).
--
-- The primary key leads with a DATE, not a timestamp, and that is the whole
-- point: a second run on the same day hits ON CONFLICT DO NOTHING instead of
-- writing a near-duplicate row a few minutes apart. Daily granularity is also
-- all the series is for — hero winrates move with patches, not with hours.
--
-- platform/gamemode/region are plain TEXT rather than enums: they are written
-- from domain enums the API already validates, and a new Blizzard region must
-- not need an ALTER TYPE migration to be recordable.
--
-- Deliberately NO foreign key anywhere. Hero keys come from heroes.csv, which
-- has no table, and rows must survive a hero being renamed or removed — the
-- history of a retired hero is exactly the part worth keeping.
CREATE TABLE IF NOT EXISTS hero_stats_snapshots (
    taken_on  DATE  NOT NULL,
    platform  TEXT  NOT NULL,
    gamemode  TEXT  NOT NULL,
    region    TEXT  NOT NULL,
    data      JSONB NOT NULL,
    PRIMARY KEY (taken_on, platform, gamemode, region)
);

-- Parsed payloads, added alongside the raw sources they are derived from.
--
-- The raw column stays: it is the only thing a new parser can be replayed
-- against, and dropping it would turn every parser change into a full refetch
-- behind the Blizzard throttle. What changes is which one the READ path uses.
-- Before this column, every cache miss rebuilt a DOM from the stored HTML —
-- 10-20ms of blocking CPU per player profile, in the event loop of a single
-- app process, for output that never varies between two identical reads.
--
-- ``data_version`` is what keeps that safe. It already existed and was written
-- but never read; it now holds app.domain.parsers.PARSER_VERSION. A row whose
-- stamp does not match is re-parsed once on read and written back, so a parser
-- change still takes effect on the very next request.
--
-- NULL is a legitimate value: it means "not parsed yet" and sends that one read
-- down the old path. That is what makes this deployable without a backfill —
-- existing rows simply upgrade themselves the first time they are served.
ALTER TABLE static_data     ADD COLUMN IF NOT EXISTS parsed JSONB;
ALTER TABLE player_profiles ADD COLUMN IF NOT EXISTS parsed JSONB;
