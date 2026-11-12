-- Optional: async materialized view that pre-aggregates hourly stats.
-- Dashboards (Grafana etc.) can query mv_region_hourly instead of scanning 5-minute data,
-- and Doris will also transparently rewrite matching queries to use it.
USE nem;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_region_hourly
BUILD IMMEDIATE
REFRESH AUTO ON SCHEDULE EVERY 1 HOUR
DISTRIBUTED BY RANDOM BUCKETS 1
PROPERTIES ("replication_num" = "1")
AS
SELECT
    date_trunc(minutes_sub(p.settlementdate, 5), 'hour') AS hour_start,
    p.regionid,
    AVG(p.rrp)                                  AS avg_rrp,
    MIN(p.rrp)                                  AS min_rrp,
    MAX(p.rrp)                                  AS max_rrp,
    SUM(CASE WHEN p.rrp < 0 THEN 1 ELSE 0 END)  AS n_negative,
    COUNT(*)                                    AS n_intervals,
    AVG(r.totaldemand)                          AS avg_demand_mw
FROM dispatch_price p
LEFT JOIN dispatch_regionsum r
       ON r.settlementdate = p.settlementdate
      AND r.regionid = p.regionid
      AND r.intervention = 0
WHERE p.intervention = 0
GROUP BY date_trunc(minutes_sub(p.settlementdate, 5), 'hour'), p.regionid;

-- Check refresh status:
--   SELECT * FROM tasks('type'='mv') WHERE MvName = 'mv_region_hourly' ORDER BY CreateTime DESC LIMIT 5;
-- Force a refresh:
--   REFRESH MATERIALIZED VIEW mv_region_hourly COMPLETE;
