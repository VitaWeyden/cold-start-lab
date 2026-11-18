-- Hourly Cloud Run cost per service/tier from the standard billing export.
-- Replace PROJECT with your project id; the table name ends with your billing account id
-- (dashes -> underscores), the wildcard below picks it up.
-- Data appears hours after the export is enabled and is NOT backfilled.
SELECT
  TIMESTAMP_TRUNC(usage_start_time, HOUR) AS hour,
  service.description AS service,
  (SELECT value FROM UNNEST(labels) WHERE key = 'idle_min') AS idle_min,
  sku.description AS sku,
  usage.unit AS unit,
  ROUND(SUM(usage.amount), 2) AS usage_amount,
  ROUND(SUM(cost), 6) AS gross_cost_eur,
  ROUND(SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 6) AS net_cost_eur
FROM `PROJECT.billing_export.gcp_billing_export_v1_*`
WHERE project.id = 'PROJECT'
  AND service.description = 'Cloud Run'
  AND usage_start_time >= TIMESTAMP('2026-10-12')
GROUP BY hour, service, idle_min, sku, unit
ORDER BY hour, idle_min, sku;
