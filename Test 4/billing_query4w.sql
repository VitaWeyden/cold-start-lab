/* Test 4 addition (always-warm, experiment=test4w): hourly cost per service and SKU.
   Includes the min-instance SKUs ("Services Min Instance CPU / Memory"), which bill for every
   second the always-warm instance exists, and the request-based SKUs of the served requests.
   Min-instance rows arrive in uneven hourly chunks (Tests 1-2: some hours report > 3,600 s), so
   the analysis sums them over the whole existence of the services, not per hour.
   Set the run date below. From Cloud Shell, in Test 4/:
     bq query --nouse_legacy_sql --format=csv --max_rows=10000 "$(cat billing_query4w.sql)" > billing4w.csv
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
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'experiment' AND value = 'test4w')
  AND usage_start_time >= TIMESTAMP('2026-11-20')  /* set to the deploy date */
GROUP BY hour, size, idle_min, sku, unit
ORDER BY hour, size, idle_min, sku
