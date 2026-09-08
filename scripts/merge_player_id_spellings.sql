-- One-off: fold the percent-encoded spelling of a Blizzard ID onto the
-- decoded one, for rows written before the storage adapter normalized them.
--
-- A Blizzard ID joins its halves with a pipe. The search endpoint publishes
-- `%7C`; a URL path decodes that back to `|`. Both spellings reached the
-- database as separate keys, so one player accumulated two of everything.
-- `fix(storage): key players on one spelling of their Blizzard ID` stops new
-- splits; this merges what is already there.
--
-- Run once, after that change is deployed:
--
--     bash scripts/backup-postgres.sh
--     ssh <host> "cd /opt/overfast-api && docker compose exec -T postgres \
--         psql -U overfast -d overfast -v ON_ERROR_STOP=1" \
--         < scripts/merge_player_id_spellings.sql
--
-- Idempotent: a second run finds no encoded rows and changes nothing. The
-- whole thing is one transaction, so a failure anywhere leaves the database
-- exactly as it was.
--
-- The three tables are treated differently on purpose, because what a lost
-- row costs differs:
--
--   player_snapshots   MERGED. A snapshot series cannot be refetched --
--                      Blizzard reports the current moment and nothing else,
--                      so a deleted row is gone for good. This is the only
--                      table where the merge is the point.
--   player_profiles    DUPLICATE DELETED. Regenerable cache; the next request
--                      refetches it. Merging two rows would mean picking a
--                      winner by freshness and writing conflict-resolution
--                      logic for data that costs one HTTP request to rebuild.
--   push_subscriptions REWRITTEN. Cheap, and it also heals by itself on the
--                      app's next launch -- doing it here just stops the
--                      poller doing double work in the meantime.

\set ON_ERROR_STOP on
\timing off

BEGIN;

\echo '=== Before ==='
SELECT 'player_snapshots' AS table_name,
       count(*) FILTER (WHERE strpos(upper(player_id), '%7C') > 0) AS encoded_rows
  FROM player_snapshots
UNION ALL
SELECT 'player_profiles',
       count(*) FILTER (WHERE strpos(upper(player_id), '%7C') > 0)
  FROM player_profiles
UNION ALL
SELECT 'push_subscriptions (arrays containing an encoded id)',
       count(*) FILTER (WHERE strpos(upper(array_to_string(player_ids, ',')), '%7C') > 0)
  FROM push_subscriptions;

-- ── player_snapshots ────────────────────────────────────────────────────────
--
-- Copy each encoded row onto the decoded key, then drop the originals.
--
-- ON CONFLICT DO NOTHING is not a shrug: the primary key is
-- (player_id, last_updated_blizzard), and last_updated_blizzard identifies a
-- Blizzard profile version. A collision therefore means both strands recorded
-- the *same* version of the same player, so the rows carry the same data and
-- either one will do. Keeping the row already on the decoded key also keeps
-- its original taken_at, which is the one the series is ordered by.
\echo '=== Merging player_snapshots ==='
INSERT INTO player_snapshots (player_id, last_updated_blizzard, taken_at, data)
SELECT replace(replace(player_id, '%7C', '|'), '%7c', '|'),
       last_updated_blizzard,
       taken_at,
       data
  FROM player_snapshots
 WHERE strpos(upper(player_id), '%7C') > 0
ON CONFLICT (player_id, last_updated_blizzard) DO NOTHING;

DELETE FROM player_snapshots
 WHERE strpos(upper(player_id), '%7C') > 0;

-- ── player_profiles ─────────────────────────────────────────────────────────
--
-- Rename where the decoded key is free, delete where it is taken. Order
-- matters: the DELETE has to run first, or the UPDATE hits the primary key.
\echo '=== Folding player_profiles ==='
DELETE FROM player_profiles enc
 WHERE strpos(upper(enc.player_id), '%7C') > 0
   AND EXISTS (
       SELECT 1 FROM player_profiles dec
        WHERE dec.player_id = replace(replace(enc.player_id, '%7C', '|'), '%7c', '|')
   );

UPDATE player_profiles
   SET player_id = replace(replace(player_id, '%7C', '|'), '%7c', '|')
 WHERE strpos(upper(player_id), '%7C') > 0;

-- ── push_subscriptions ──────────────────────────────────────────────────────
--
-- Normalize and de-duplicate each roster. array_agg(DISTINCT ...) reorders,
-- which is fine: the column is a set of watched players, and nothing reads it
-- positionally. IS DISTINCT FROM keeps the rewrite off rows already correct.
\echo '=== Rewriting push_subscriptions rosters ==='
UPDATE push_subscriptions s
   SET player_ids = sub.ids
  FROM (
      SELECT token, array_agg(DISTINCT pid) AS ids
        FROM (
            SELECT token,
                   replace(replace(unnest(player_ids), '%7C', '|'), '%7c', '|') AS pid
              FROM push_subscriptions
        ) normalized
       GROUP BY token
  ) sub
 WHERE s.token = sub.token
   AND s.player_ids IS DISTINCT FROM sub.ids;

\echo '=== After (all three must read 0) ==='
SELECT 'player_snapshots' AS table_name,
       count(*) FILTER (WHERE strpos(upper(player_id), '%7C') > 0) AS encoded_rows
  FROM player_snapshots
UNION ALL
SELECT 'player_profiles',
       count(*) FILTER (WHERE strpos(upper(player_id), '%7C') > 0)
  FROM player_profiles
UNION ALL
SELECT 'push_subscriptions (arrays containing an encoded id)',
       count(*) FILTER (WHERE strpos(upper(array_to_string(player_ids, ',')), '%7C') > 0)
  FROM push_subscriptions;

\echo '=== Merged snapshot series ==='
SELECT player_id, count(*) AS snapshots, max(taken_at) AS newest
  FROM player_snapshots
 GROUP BY player_id
HAVING count(*) > 1
 ORDER BY count(*) DESC
 LIMIT 20;

COMMIT;
