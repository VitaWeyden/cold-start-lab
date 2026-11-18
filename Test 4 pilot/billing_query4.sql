-- Test 4 / pilot: hourly Cloud Run cost per service from the standard billing export.
-- Replace PROJECT with your project id. The labels come from deploy4p.sh
-- (experiment=pilot4, max_inst, replica); for Test 4 change the experiment value.
-- Rows appear hours after usage; run it the day after the measurement.
SELECT
  TIMESTAMP_TRUNC(usage_start_time, HOUR) AS hour,
  (SELECT value FROM UNNEST(labels) WHERE key = 'max_inst') AS max_inst,
  (SELECT value FROM UNNEST(labels) WHERE key = 'replica') AS replica,
  sku.description AS sku,
  usage.unit AS unit,
  ROUND(SUM(usage.amount), 2) AS usage_amount,
  ROUND(SUM(cost), 6) AS gross_cost_eur,
  ROUND(SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 6) AS net_cost_eur
FROM `PROJECT.billing_export.gcp_billing_export_v1_*`
WHERE project.id = 'PROJECT'
  AND service.description = 'Cloud Run'
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'experiment' AND value = 'pilot4')
  AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY hour, max_inst, replica, sku, unit
ORDER BY hour, max_inst, replica, sku;

-- Sanity check without the label filter (did the labels arrive at all?):
-- SELECT l.key, l.value, ROUND(SUM(cost), 6) AS cost
-- FROM `PROJECT.billing_export.gcp_billing_export_v1_*`, UNNEST(labels) l
-- WHERE service.description = 'Cloud Run' AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
-- GROUP BY 1, 2 ORDER BY 1, 2;
