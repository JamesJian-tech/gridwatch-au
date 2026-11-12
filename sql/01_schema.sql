-- AEMO NEM on Apache Doris 4.x — schema
-- Run:  mysql -h 127.0.0.1 -P 9030 -uroot -p < sql/01_schema.sql
--
-- Conventions
--   * settlementdate is the END of the 5-minute dispatch interval, in NEM time
--     (AEST, UTC+10, no daylight saving). 22:20 means 22:15–22:20.
--   * UNIQUE KEY + merge-on-write: re-loading the same interval overwrites, so the
--     Current/ and Archive/ feeds can overlap safely.
--   * Monthly AUTO PARTITION: partitions are created on first write, so old months
--     can be dropped or moved to cold storage independently.
--   * replication_num = 1 because this is a single-BE home cluster.

CREATE DATABASE IF NOT EXISTS nem;
USE nem;

-- 5-minute regional prices (energy + FCAS), DISPATCH.PRICE
CREATE TABLE IF NOT EXISTS dispatch_price (
    settlementdate      DATETIME      NOT NULL COMMENT 'interval end, NEM time (AEST)',
    regionid            VARCHAR(10)   NOT NULL COMMENT 'NSW1 QLD1 VIC1 SA1 TAS1',
    intervention        TINYINT       NOT NULL COMMENT '0 = pricing run (use this for prices)',
    rrp                 DECIMAL(15,5) NULL COMMENT 'regional reference price, $/MWh',
    rop                 DECIMAL(15,5) NULL COMMENT 'original price before adjustment',
    apcflag             TINYINT       NULL,
    marketsuspendedflag TINYINT       NULL,
    raise6secrrp        DECIMAL(15,5) NULL,
    raise60secrrp       DECIMAL(15,5) NULL,
    raise5minrrp        DECIMAL(15,5) NULL,
    raiseregrrp         DECIMAL(15,5) NULL,
    lower6secrrp        DECIMAL(15,5) NULL,
    lower60secrrp       DECIMAL(15,5) NULL,
    lower5minrrp        DECIMAL(15,5) NULL,
    lowerregrrp         DECIMAL(15,5) NULL,
    raise1secrrp        DECIMAL(15,5) NULL,
    lower1secrrp        DECIMAL(15,5) NULL,
    price_status        VARCHAR(20)   NULL COMMENT 'FIRM / NOT FIRM',
    lastchanged         DATETIME      NULL
)
UNIQUE KEY(settlementdate, regionid, intervention)
AUTO PARTITION BY RANGE (date_trunc(settlementdate, 'month')) ()
DISTRIBUTED BY HASH(regionid) BUCKETS 1
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);

-- 5-minute regional demand / supply summary, DISPATCH.REGIONSUM
CREATE TABLE IF NOT EXISTS dispatch_regionsum (
    settlementdate              DATETIME      NOT NULL,
    regionid                    VARCHAR(10)   NOT NULL,
    intervention                TINYINT       NOT NULL,
    totaldemand                 DECIMAL(15,5) NULL COMMENT 'operational demand, MW',
    availablegeneration         DECIMAL(15,5) NULL,
    availableload               DECIMAL(15,5) NULL,
    demandforecast              DECIMAL(15,5) NULL,
    dispatchablegeneration      DECIMAL(15,5) NULL,
    dispatchableload            DECIMAL(15,5) NULL,
    netinterchange              DECIMAL(15,5) NULL COMMENT 'net export (+) / import (-), MW',
    excessgeneration            DECIMAL(15,5) NULL,
    clearedsupply               DECIMAL(15,5) NULL,
    totalintermittentgeneration DECIMAL(15,5) NULL,
    demand_and_nonschedgen      DECIMAL(15,5) NULL,
    uigf                        DECIMAL(15,5) NULL COMMENT 'unconstrained intermittent gen forecast',
    semischedule_clearedmw      DECIMAL(15,5) NULL,
    semischedule_compliancemw   DECIMAL(15,5) NULL,
    ss_solar_uigf               DECIMAL(15,5) NULL,
    ss_wind_uigf                DECIMAL(15,5) NULL,
    ss_solar_clearedmw          DECIMAL(15,5) NULL,
    ss_wind_clearedmw           DECIMAL(15,5) NULL,
    bdu_energy_storage          DECIMAL(15,5) NULL COMMENT 'battery state of charge, MWh',
    bdu_clearedmw_gen           DECIMAL(15,5) NULL,
    bdu_clearedmw_load          DECIMAL(15,5) NULL,
    lastchanged                 DATETIME      NULL
)
UNIQUE KEY(settlementdate, regionid, intervention)
AUTO PARTITION BY RANGE (date_trunc(settlementdate, 'month')) ()
DISTRIBUTED BY HASH(regionid) BUCKETS 1
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);

-- 5-minute interconnector flows, DISPATCH.INTERCONNECTORRES
-- Positive flow = from the first region in the ID to the second (VIC1-NSW1 > 0 means VIC -> NSW).
CREATE TABLE IF NOT EXISTS dispatch_interconnectorres (
    settlementdate   DATETIME      NOT NULL,
    interconnectorid VARCHAR(20)   NOT NULL,
    intervention     TINYINT       NOT NULL,
    meteredmwflow    DECIMAL(15,5) NULL,
    mwflow           DECIMAL(15,5) NULL,
    mwlosses         DECIMAL(15,5) NULL,
    marginalvalue    DECIMAL(15,5) NULL,
    exportlimit      DECIMAL(15,5) NULL,
    importlimit      DECIMAL(15,5) NULL,
    marginalloss     DECIMAL(15,5) NULL,
    lastchanged      DATETIME      NULL
)
UNIQUE KEY(settlementdate, interconnectorid, intervention)
AUTO PARTITION BY RANGE (date_trunc(settlementdate, 'month')) ()
DISTRIBUTED BY HASH(interconnectorid) BUCKETS 1
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);

-- 5-minute output per generating unit, DISPATCH.UNIT_SCADA  (~50M rows / year)
-- Batteries registered as bidirectional units are negative while charging.
CREATE TABLE IF NOT EXISTS unit_scada (
    settlementdate DATETIME      NOT NULL,
    duid           VARCHAR(20)   NOT NULL,
    scadavalue     DECIMAL(16,6) NULL COMMENT 'MW',
    lastchanged    DATETIME      NULL
)
UNIQUE KEY(settlementdate, duid)
AUTO PARTITION BY RANGE (date_trunc(settlementdate, 'month')) ()
DISTRIBUTED BY HASH(duid) BUCKETS 2
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);

-- Generator metadata, loaded from AEMO's NEM Registration and Exemption List
CREATE TABLE IF NOT EXISTS duid_info (
    duid                       VARCHAR(20)  NOT NULL,
    station_name               VARCHAR(200) NULL,
    participant                VARCHAR(200) NULL,
    region                     VARCHAR(10)  NULL,
    dispatch_type              VARCHAR(50)  NULL,
    classification             VARCHAR(50)  NULL,
    fuel_source_primary        VARCHAR(100) NULL,
    fuel_source_descriptor     VARCHAR(100) NULL,
    technology_type_primary    VARCHAR(100) NULL,
    technology_type_descriptor VARCHAR(100) NULL,
    reg_cap_mw                 DOUBLE       NULL,
    max_cap_mw                 DOUBLE       NULL,
    fuel_group                 VARCHAR(30)  NULL COMMENT 'Black coal / Brown coal / Gas / Hydro / Wind / Solar / Battery / ...'
)
UNIQUE KEY(duid)
DISTRIBUTED BY HASH(duid) BUCKETS 1
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);

-- Which NEMWEB files have been loaded (lets the collector skip them)
CREATE TABLE IF NOT EXISTS ingest_log (
    source        VARCHAR(20)  NOT NULL,
    file_name     VARCHAR(200) NOT NULL,
    file_ts       DATETIME     NULL COMMENT 'timestamp in the file name, NEM time',
    n_rows        INT          NULL,
    loaded_at_utc DATETIME     NULL
)
UNIQUE KEY(source, file_name)
DISTRIBUTED BY HASH(file_name) BUCKETS 1
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);
