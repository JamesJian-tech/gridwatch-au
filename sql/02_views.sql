-- Convenience views. Run after 01_schema.sql.
USE nem;

-- Prices from the pricing run only, with interval start and calendar fields (NEM time).
DROP VIEW IF EXISTS v_price;
CREATE VIEW v_price AS
SELECT
    p.settlementdate                          AS interval_end,
    minutes_sub(p.settlementdate, 5)          AS interval_start,
    DATE(minutes_sub(p.settlementdate, 5))    AS nem_date,
    HOUR(minutes_sub(p.settlementdate, 5))    AS nem_hour,
    p.regionid,
    p.rrp,
    p.price_status
FROM dispatch_price p
WHERE p.intervention = 0;

-- Price + demand per region per interval.
DROP VIEW IF EXISTS v_region_5min;
CREATE VIEW v_region_5min AS
SELECT
    p.settlementdate                       AS interval_end,
    DATE(minutes_sub(p.settlementdate, 5)) AS nem_date,
    HOUR(minutes_sub(p.settlementdate, 5)) AS nem_hour,
    p.regionid,
    p.rrp,
    r.totaldemand,
    r.availablegeneration,
    r.netinterchange,
    r.ss_solar_clearedmw,
    r.ss_wind_clearedmw
FROM dispatch_price p
LEFT JOIN dispatch_regionsum r
       ON r.settlementdate = p.settlementdate
      AND r.regionid       = p.regionid
      AND r.intervention   = 0
WHERE p.intervention = 0;

-- Unit output tagged with region and fuel group (needs duid_info loaded).
-- gen_mw is output >= 0; charge_mw is battery / pumped-hydro consumption (as a positive number).
DROP VIEW IF EXISTS v_unit_5min;
CREATE VIEW v_unit_5min AS
SELECT
    s.settlementdate                       AS interval_end,
    DATE(minutes_sub(s.settlementdate, 5)) AS nem_date,
    HOUR(minutes_sub(s.settlementdate, 5)) AS nem_hour,
    s.duid,
    d.station_name,
    d.region                               AS regionid,
    COALESCE(d.fuel_group, 'Unknown')      AS fuel_group,
    GREATEST(s.scadavalue, 0)              AS gen_mw,
    GREATEST(-s.scadavalue, 0)             AS charge_mw,
    s.scadavalue                           AS mw
FROM unit_scada s
LEFT JOIN duid_info d ON d.duid = s.duid;
