-- migration_phase3_v4f_review_count_sync.sql
-- One-shot retroactive fix for source_records.review_count desync.
--
-- Sources affected (review_count was NULL while reviews existed in DB):
--   caramaps, womostell, vansite, campspace, wtmg, furgovw
--
-- These scrapers don't set num_reviews in normalize() (no API signal), and the
-- download_reviews path didn't refresh review_count after upsert. Forward fix
-- is in scraper code (db.refresh_review_count() now called post-upsert).
-- This migration backfills the existing rows.
--
-- Safe: uses GREATEST so it never DECREASES an API-provided expected count.
-- Idempotent: re-running is a no-op.

\timing on

BEGIN;

WITH counts AS (
    SELECT source, spot_id, COUNT(*) AS cnt
    FROM reviews
    WHERE source IN ('caramaps','womostell','vansite','campspace','wtmg','furgovw')
    GROUP BY source, spot_id
)
UPDATE source_records sr
SET review_count = GREATEST(COALESCE(sr.review_count, 0), counts.cnt::int)
FROM counts
WHERE sr.source = counts.source
  AND sr.spot_id = counts.spot_id;

-- Also sync spots.total_reviews for these (count across ALL sources per spot)
WITH spot_totals AS (
    SELECT spot_id, COUNT(*) AS cnt
    FROM reviews
    GROUP BY spot_id
)
UPDATE spots s
SET total_reviews = GREATEST(COALESCE(s.total_reviews, 0), st.cnt::int)
FROM spot_totals st
WHERE s.id = st.spot_id;

COMMIT;

-- Sanity check after migration
SELECT 'After migration counts' AS section;
SELECT source,
       COUNT(*) FILTER (WHERE review_count > 0) AS spots_con_review_count_pos,
       SUM(review_count) AS sum_rc
FROM source_records
WHERE source IN ('caramaps','womostell','vansite','campspace','wtmg','furgovw')
GROUP BY source
ORDER BY source;
