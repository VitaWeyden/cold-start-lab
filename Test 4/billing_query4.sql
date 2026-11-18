/* Test 4: hourly Cloud Run cost per service (size x idle tier) from the standard billing export.
   Labels from deploy4.sh: experiment=test4, size (s/m/l), idle_min (05, 14, 15, 16, 18, 20).
   Rows appear hours after usage; run it the day after the measurement.
   The comment is a block comment on purpose: `bq query` reads a leading `--` line as a flag
   (pilot lesson). Set the run date below instead of a "last N days" window (pilot lesson:
   the 7-day window was already too short 12 days after the run).

   From Cloud Shell, in Test 4/:
     bq query --nouse_legacy_sql --format=csv --max_rows=10000 "$(cat billing_query4.sql)" > billing4.csv
*/
SELECT
  TIMESTAMP_TRUNC(usage_start_time, HOUR) AS hour,
  (SELECT value FROM UNNEST(labels) WHERE key = 'size') AS size,
  (SELECT value FROM UNNEST(labels) WHERE key = 'idle_min') AS idle_min,
  sku.description AS sku,
  usage.unit AS unit,
  ROUND(SUM(usage.amount), 2) AS usage_amount,
  ROUND(SUM(cost), 6) AS gross_cost_eur,
  ROUND(SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 6) AS net_cost_eur
FROM `cold-start-lab.billing_export.gcp_billing_export_v1_*`
WHERE project.id = 'cold-start-lab'
  AND service.description = 'Cloud Run'
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'experiment' AND value = 'test4')
  AND usage_start_time >= TIMESTAMP('2026-11-01')  /* set to the run date */
GROUP BY hour, size, idle_min, sku, unit
ORDER BY hour, size, idle_min, sku
