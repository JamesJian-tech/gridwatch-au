-- Example analyses. All times are NEM time (AEST, UTC+10). Melbourne in summer (AEDT)
-- is one hour ahead of these timestamps.
USE nem;

-- 1. Latest price and demand in every region
SELECT regionid, interval_end, rrp, totaldemand
FROM v_region_5min
WHERE interval_end = (SELECT MAX(settlementdate) FROM dispatch_price);

-- 2. Daily price statistics, last 30 days (each 5-min interval weighted equally)
SELECT nem_date, regionid,
       ROUND(AVG(rrp), 2)                                     AS avg_rrp,
       ROUND(MIN(rrp), 2)                                     AS min_rrp,
       ROUND(MAX(rrp), 2)                                     AS max_rrp,
       ROUND(STDDEV(rrp), 2)                                  AS sd_rrp,
       ROUND(100 * AVG(CASE WHEN rrp < 0   THEN 1 ELSE 0 END), 1) AS pct_negative,
       SUM(CASE WHEN rrp > 300 THEN 1 ELSE 0 END)             AS n_over_300
FROM v_price
WHERE interval_end >= DATE_SUB(NOW(), INTERVAL 30 DAY)
GROUP BY nem_date, regionid
ORDER BY nem_date DESC, regionid;

-- 3. When do negative prices happen?  Share of negative intervals by hour of day, per region
--    (feed this into a heatmap: x = hour, y = region)
SELECT regionid, nem_hour,
       ROUND(100 * AVG(CASE WHEN rrp < 0 THEN 1 ELSE 0 END), 1) AS pct_negative,
       ROUND(AVG(rrp), 2)                                   AS avg_rrp
FROM v_price
WHERE interval_end >= DATE_SUB(NOW(), INTERVAL 90 DAY)
GROUP BY regionid, nem_hour
ORDER BY regionid, nem_hour;

-- 4. Monthly trend of negative-price share (needs a few months of backfill)
SELECT DATE_FORMAT(nem_date, '%Y-%m') AS month, regionid,
       ROUND(100 * AVG(CASE WHEN rrp < 0 THEN 1 ELSE 0 END), 1) AS pct_negative,
       ROUND(AVG(rrp), 2) AS avg_rrp
FROM v_price
GROUP BY DATE_FORMAT(nem_date, '%Y-%m'), regionid
ORDER BY month, regionid;

-- 5. Price spikes: every interval above $1,000/MWh in the last 30 days
SELECT interval_end, regionid, rrp, price_status
FROM v_price
WHERE rrp > 1000 AND interval_end >= DATE_SUB(NOW(), INTERVAL 30 DAY)
ORDER BY rrp DESC
LIMIT 100;

-- 6. Evening peak premium: average 17:00–21:00 price minus 10:00–15:00 price, per day
--    (a rough proxy for how much a 2-4h battery could earn by shifting solar into the evening)
SELECT nem_date, regionid,
       ROUND(AVG(CASE WHEN nem_hour BETWEEN 17 AND 20 THEN rrp END)
           - AVG(CASE WHEN nem_hour BETWEEN 10 AND 14 THEN rrp END), 2) AS evening_minus_midday
FROM v_price
WHERE interval_end >= DATE_SUB(NOW(), INTERVAL 60 DAY)
GROUP BY nem_date, regionid
ORDER BY nem_date DESC, regionid;

-- 7. Price vs demand: average price in each demand decile, per region
WITH d AS (
    SELECT regionid, rrp, totaldemand,
           NTILE(10) OVER (PARTITION BY regionid ORDER BY totaldemand) AS demand_decile
    FROM v_region_5min
    WHERE interval_end >= DATE_SUB(NOW(), INTERVAL 90 DAY) AND totaldemand IS NOT NULL
)
SELECT regionid, demand_decile,
       ROUND(MIN(totaldemand)) AS demand_from_mw,
       ROUND(MAX(totaldemand)) AS demand_to_mw,
       ROUND(AVG(rrp), 2)      AS avg_rrp
FROM d
GROUP BY regionid, demand_decile
ORDER BY regionid, demand_decile;

-- 8. Biggest 5-minute price jumps (volatility), last 7 days
SELECT * FROM (
    SELECT regionid, interval_end, rrp,
           rrp - LAG(rrp) OVER (PARTITION BY regionid ORDER BY interval_end) AS jump
    FROM v_price
    WHERE interval_end >= DATE_SUB(NOW(), INTERVAL 7 DAY)
) t
WHERE jump IS NOT NULL
ORDER BY ABS(jump) DESC
LIMIT 50;

-- ---------- Needs unit_scada + duid_info -----------------------------------

-- 9. Victoria's generation mix by hour of day, last 7 days (average MW)
SELECT nem_hour, fuel_group, ROUND(SUM(gen_mw) / COUNT(DISTINCT interval_end)) AS avg_mw
FROM v_unit_5min
WHERE regionid = 'VIC1' AND interval_end >= DATE_SUB(NOW(), INTERVAL 7 DAY)
GROUP BY nem_hour, fuel_group
ORDER BY nem_hour, avg_mw DESC;

-- 10. Daily renewable share of scheduled + semi-scheduled output, per region
--     (rooftop solar is not in SCADA, so this understates true renewable share)
SELECT nem_date, regionid,
       ROUND(100 * SUM(CASE WHEN fuel_group IN ('Wind', 'Solar', 'Hydro', 'Bioenergy') THEN gen_mw ELSE 0 END)
                 / NULLIF(SUM(CASE WHEN fuel_group <> 'Battery' THEN gen_mw ELSE 0 END), 0), 1) AS pct_renewable
FROM v_unit_5min
WHERE interval_end >= DATE_SUB(NOW(), INTERVAL 30 DAY) AND regionid IS NOT NULL
GROUP BY nem_date, regionid
ORDER BY nem_date DESC, regionid;

-- 11. Battery energy-arbitrage estimate per unit, last 30 days
--     revenue ≈ Σ MW × $/MWh × (5/60) h. Ignores FCAS revenue, losses and loss factors.
SELECT u.duid, u.station_name, u.regionid,
       ROUND(SUM(u.gen_mw)    * 5 / 60)              AS discharged_mwh,
       ROUND(SUM(u.charge_mw) * 5 / 60)              AS charged_mwh,
       ROUND(SUM(u.mw * p.rrp) * 5 / 60)             AS net_energy_revenue_aud,
       ROUND(SUM(u.gen_mw * p.rrp) / NULLIF(SUM(u.gen_mw), 0), 2)       AS avg_sell_price,
       ROUND(SUM(u.charge_mw * p.rrp) / NULLIF(SUM(u.charge_mw), 0), 2) AS avg_buy_price
FROM v_unit_5min u
JOIN v_price p ON p.interval_end = u.interval_end AND p.regionid = u.regionid
WHERE u.fuel_group = 'Battery' AND u.interval_end >= DATE_SUB(NOW(), INTERVAL 30 DAY)
GROUP BY u.duid, u.station_name, u.regionid
ORDER BY net_energy_revenue_aud DESC;

-- 12. Coal output vs price in VIC: how much do brown-coal units back off at negative prices?
SELECT CASE WHEN p.rrp < 0 THEN 'negative' WHEN p.rrp < 50 THEN '0-50' WHEN p.rrp < 150 THEN '50-150' ELSE '150+' END AS price_band,
       ROUND(SUM(u.gen_mw) / COUNT(DISTINCT u.interval_end)) AS avg_brown_coal_mw,
       COUNT(DISTINCT u.interval_end) AS n_intervals
FROM v_unit_5min u
JOIN v_price p ON p.interval_end = u.interval_end AND p.regionid = 'VIC1'
WHERE u.regionid = 'VIC1' AND u.fuel_group = 'Brown coal'
  AND u.interval_end >= DATE_SUB(NOW(), INTERVAL 90 DAY)
GROUP BY price_band
ORDER BY MIN(p.rrp);

-- ---------- Interconnectors -------------------------------------------------

-- 13. VIC <-> NSW flow by hour of day, last 30 days (positive = VIC exporting to NSW)
SELECT HOUR(minutes_sub(settlementdate, 5)) AS nem_hour,
       ROUND(AVG(meteredmwflow)) AS avg_mw,
       ROUND(MIN(meteredmwflow)) AS min_mw,
       ROUND(MAX(meteredmwflow)) AS max_mw
FROM dispatch_interconnectorres
WHERE interconnectorid = 'VIC1-NSW1' AND intervention = 0
  AND settlementdate >= DATE_SUB(NOW(), INTERVAL 30 DAY)
GROUP BY nem_hour
ORDER BY nem_hour;

-- 14. Price separation: intervals where VIC and NSW prices differ by > $100 (binding constraints)
SELECT v.interval_end, v.rrp AS vic_rrp, n.rrp AS nsw_rrp, i.meteredmwflow AS vic_to_nsw_mw,
       i.exportlimit, i.importlimit
FROM v_price v
JOIN v_price n ON n.interval_end = v.interval_end AND n.regionid = 'NSW1'
JOIN dispatch_interconnectorres i
  ON i.settlementdate = v.interval_end AND i.interconnectorid = 'VIC1-NSW1' AND i.intervention = 0
WHERE v.regionid = 'VIC1' AND ABS(v.rrp - n.rrp) > 100
  AND v.interval_end >= DATE_SUB(NOW(), INTERVAL 30 DAY)
ORDER BY ABS(v.rrp - n.rrp) DESC
LIMIT 100;

-- ---------- Housekeeping ----------------------------------------------------

-- 15. Data completeness: intervals per day should be 288 per region
SELECT nem_date, regionid, COUNT(*) AS n_intervals
FROM v_price
GROUP BY nem_date, regionid
HAVING COUNT(*) <> 288
ORDER BY nem_date DESC;

-- 16. Collector progress
SELECT source, COUNT(*) AS files, MAX(file_ts) AS latest_file, SUM(n_rows) AS rows_loaded
FROM ingest_log GROUP BY source;
