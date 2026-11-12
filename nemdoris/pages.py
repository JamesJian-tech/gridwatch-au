"""Static dashboard for GitHub Pages: NEMWEB -> small CSV store -> one dashboard JSON.

Runs without Doris (on a GitHub Actions runner). Only DispatchIS is used: regional
price, demand, semi-scheduled solar/wind, battery, and interconnector flows.

Store layout (kept on the `site-data` branch, force-pushed as a single commit):
  regions/YYYY-MM-DD.csv          5-min rows per region, intervention = 0
  interconnectors/YYYY-MM-DD.csv  5-min rows per interconnector, intervention = 0
  state.json                      {"last_file_ts": "YYYY-MM-DD HH:MM:SS"}

A NEM day holds the 288 intervals that END at 00:05 .. 24:00 (files are bucketed by
interval start). All times are NEM time (AEST, UTC+10, no daylight saving).
"""
from __future__ import annotations

import csv
import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import aemo
from .schema import MMS_TABLES, to_doris_rows

log = logging.getLogger(__name__)

TS_FMT = "%Y-%m-%d %H:%M:%S"
REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]
INTERVALS_PER_DAY = 288
RECENT_HOURS = 48
PROFILE_DAYS = 30

REGION_FIELDS = ["settlementdate", "regionid", "rrp", "totaldemand", "netinterchange",
                 "ss_solar_clearedmw", "ss_wind_clearedmw", "bdu_clearedmw_gen", "bdu_clearedmw_load"]
IC_FIELDS = ["settlementdate", "interconnectorid", "meteredmwflow", "exportlimit", "importlimit"]
REGION_KEY = ("settlementdate", "regionid")
IC_KEY = ("settlementdate", "interconnectorid")


def nem_day(settlementdate: str) -> str:
    """NEM day of a 5-min interval, given its END time ('2026-11-12 00:00:00' -> '2026-11-11')."""
    return (datetime.strptime(settlementdate, TS_FMT) - timedelta(minutes=5)).strftime("%Y-%m-%d")


def parse_since(value: str, today: date) -> date:
    """'2026-10-01' -> that date; '90' -> 90 days before today."""
    v = value.strip()
    if v.isdigit():
        return today - timedelta(days=int(v))
    return datetime.strptime(v, "%Y-%m-%d").date()


# ---------------------------------------------------------------- extract

def extract(data: bytes) -> tuple[list[dict], list[dict]]:
    """DispatchIS zip -> (region rows, interconnector rows), pricing run only."""
    tables = aemo.parse_zip(data)

    def rows(key):
        out, _ = to_doris_rows(MMS_TABLES[key], tables.get(key, []))
        return [r for r in out if r.get("intervention") == 0]

    sums = {(r["settlementdate"], r["regionid"]): r for r in rows(("DISPATCH", "REGIONSUM"))}
    regions = []
    for p in rows(("DISPATCH", "PRICE")):
        s = sums.get((p["settlementdate"], p["regionid"]), {})
        regions.append({
            "settlementdate": p["settlementdate"], "regionid": p["regionid"], "rrp": p["rrp"],
            **{f: s.get(f) for f in REGION_FIELDS[3:]},
        })
    ics = [{f: r.get(f) for f in IC_FIELDS} for r in rows(("DISPATCH", "INTERCONNECTORRES"))]
    return regions, ics


# ---------------------------------------------------------------- store

def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        s = f"{v:.3f}".rstrip("0").rstrip(".")
        return "0" if s in ("", "-0") else s
    return str(v)


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, kind: str, day: str) -> Path:
        return self.root / kind / f"{day}.csv"

    def read_day(self, kind: str, day: str) -> list[dict]:
        p = self._path(kind, day)
        if not p.exists():
            return []
        with p.open(newline="") as f:
            return list(csv.DictReader(f))

    def days(self, kind: str) -> list[str]:
        d = self.root / kind
        return sorted(p.stem for p in d.glob("*.csv")) if d.exists() else []

    def upsert(self, kind: str, fields: list[str], key: tuple, rows: list[dict]) -> set[str]:
        """Merge rows into their day files (same key -> newer row wins). Returns touched days."""
        by_day: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_day[nem_day(r["settlementdate"])].append(r)
        for day, new in by_day.items():
            merged = {tuple(r[k] for k in key): r for r in self.read_day(kind, day)}
            for r in new:
                merged[tuple(r[k] for k in key)] = {f: _fmt(r.get(f)) for f in fields}
            p = self._path(kind, day)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with tmp.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(merged[k] for k in sorted(merged))
            tmp.replace(p)
        return set(by_day)

    def day_complete(self, day: str) -> bool:
        return len(self.read_day("regions", day)) >= INTERVALS_PER_DAY * len(REGIONS)

    def load_state(self) -> dict:
        p = self.root / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def save_state(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "state.json").write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")

    def ingest(self, data: bytes) -> int:
        regions, ics = extract(data)
        self.upsert("regions", REGION_FIELDS, REGION_KEY, regions)
        self.upsert("interconnectors", IC_FIELDS, IC_KEY, ics)
        return len(regions)


# ---------------------------------------------------------------- update from NEMWEB

def update(store: Store, session, since: date | None = None, max_files: int | None = None,
           pause: float = 1.0) -> dict:
    """Pull new Current/ files, plus Archive/ days for `since` and for any gap. Returns stats.

    NEMWEB answers 403 when hit a few times a second, so every download is followed by
    `pause` seconds, and whole days already complete in the store are never re-fetched
    from Current/ (one Archive file replaces up to 288 Current files).
    """
    src = aemo.SOURCES["dispatchis"]
    state = store.load_state()
    last = datetime.strptime(state["last_file_ts"], TS_FMT) if state.get("last_file_ts") else None
    current = aemo.fetch_listing(session, src.current_dir, src.current_re)
    stats = {"archive_files": 0, "current_files": 0, "skipped_complete": 0, "failed": 0}

    want: set[date] = set()
    today = aemo.nem_now().date()
    if since is None and last is None:
        since = today - timedelta(days=2)  # first run: earlier days come cheaper from Archive/
    if since:
        want |= {since + timedelta(days=i) for i in range((today - since).days + 1)}
    if last and current and current[0][1] > last + timedelta(minutes=5):
        gap_from = last.date()
        want |= {gap_from + timedelta(days=i) for i in range((current[0][1].date() - gap_from).days + 1)}
        log.warning("gap since %s is older than Current/ (%s); filling from Archive", last, current[0][1])
    if want:
        archive = aemo.fetch_listing(session, src.archive_dir, src.archive_re)
        todo = [(n, ts) for n, ts in archive if ts.date() in want and not store.day_complete(ts.strftime("%Y-%m-%d"))]
        log.info("archive: %d day file(s) to load", len(todo))
        for name, _ts in todo:
            try:
                store.ingest(aemo.download(session, src.archive_dir, name))
                stats["archive_files"] += 1
                log.info("archive %s", name)
            except Exception as e:  # keep going; the next run retries incomplete days
                stats["failed"] += 1
                log.error("archive %s failed: %s", name, e)
            time.sleep(pause)

    todo = [(n, ts) for n, ts in current if last is None or ts > last]
    if max_files:
        todo = todo[-max_files:]
    log.info("current: %d new file(s)", len(todo))
    complete: dict[str, bool] = {}
    try:
        for name, ts in todo:
            day = nem_day(ts.strftime(TS_FMT))  # file stamp = interval end
            if day not in complete:
                complete[day] = store.day_complete(day)
            if complete[day]:
                state["last_file_ts"] = ts.strftime(TS_FMT)
                stats["skipped_complete"] += 1
                continue
            try:
                store.ingest(aemo.download(session, src.current_dir, name))
            except Exception as e:  # stop here so the next run retries from this file
                stats["failed"] += 1
                log.error("current %s failed: %s", name, e)
                break
            state["last_file_ts"] = ts.strftime(TS_FMT)
            stats["current_files"] += 1
            time.sleep(pause)
    finally:
        store.save_state(state)
    return stats


# ---------------------------------------------------------------- dashboard JSON

def _num(s: str):
    return float(s) if s not in ("", None) else None


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _short(ts: str) -> str:
    return ts[:16]  # 'YYYY-MM-DD HH:MM'


def build(store: Store, generated_at: datetime | None = None) -> dict:
    generated_at = generated_at or datetime.now(timezone.utc)
    rows = []
    for day in store.days("regions"):
        for r in store.read_day("regions", day):
            rrp = _num(r["rrp"])
            if rrp is None or r["regionid"] not in REGIONS:
                continue
            rows.append((r["settlementdate"], r["regionid"], rrp, _num(r["totaldemand"]),
                         _num(r["ss_solar_clearedmw"]), _num(r["ss_wind_clearedmw"]),
                         _num(r["netinterchange"]), day))
    out = {
        "generated_at_utc": generated_at.strftime("%Y-%m-%d %H:%M"),
        "timezone": "NEM time (AEST, UTC+10)",
        "regions": REGIONS,
    }
    if not rows:
        out["empty"] = True
        return out

    last_ts = max(r[0] for r in rows)
    out["last_interval"] = _short(last_ts)
    last_dt = datetime.strptime(last_ts, TS_FMT)

    # latest tile per region
    latest = {}
    for ts, reg, rrp, dem, sol, wind, net, _ in rows:
        if reg not in latest or ts > latest[reg]["ts"]:
            latest[reg] = {"ts": ts, "rrp": rrp, "demand": dem, "solar": sol, "wind": wind, "net": net}
    out["latest"] = {reg: {"t": _short(v["ts"]), "rrp": _r(v["rrp"]), "demand": _r(v["demand"], 0),
                           "solar": _r(v["solar"], 0), "wind": _r(v["wind"], 0), "net": _r(v["net"], 0)}
                     for reg, v in latest.items()}

    # last 48 h, 5-min, aligned on one time axis
    cut = (last_dt - timedelta(hours=RECENT_HOURS)).strftime(TS_FMT)
    recent = [r for r in rows if r[0] > cut]
    axis = sorted({r[0] for r in recent})
    pos = {t: i for i, t in enumerate(axis)}
    series = {reg: {k: [None] * len(axis) for k in ("rrp", "demand", "solar", "wind")} for reg in REGIONS}
    for ts, reg, rrp, dem, sol, wind, *_ in recent:
        i = pos[ts]
        s = series[reg]
        s["rrp"][i], s["demand"][i], s["solar"][i], s["wind"][i] = _r(rrp), _r(dem, 0), _r(sol, 0), _r(wind, 0)
    out["recent"] = {"t": [_short(t) for t in axis], "series": series}

    # daily stats per region
    daily: dict = defaultdict(lambda: {"rrp": [], "dem": 0.0, "ren": 0.0, "dem_n": 0})
    for ts, reg, rrp, dem, sol, wind, _net, day in rows:
        d = daily[(day, reg)]
        d["rrp"].append(rrp)
        if dem:
            d["dem"] += dem
            d["ren"] += (sol or 0) + (wind or 0)
            d["dem_n"] += 1
    days = sorted({k[0] for k in daily})
    dstats = {reg: {k: [] for k in ("avg", "min", "max", "neg_pct", "n_300", "renew_pct", "n")} for reg in REGIONS}
    for day in days:
        for reg in REGIONS:
            d = daily.get((day, reg))
            s = dstats[reg]
            if not d:
                for k in s:
                    s[k].append(None)
                continue
            p = d["rrp"]
            s["avg"].append(_r(sum(p) / len(p)))
            s["min"].append(_r(min(p)))
            s["max"].append(_r(max(p)))
            s["neg_pct"].append(_r(100 * sum(1 for x in p if x < 0) / len(p), 1))
            s["n_300"].append(sum(1 for x in p if x >= 300))
            s["renew_pct"].append(_r(100 * d["ren"] / d["dem"], 1) if d["dem"] else None)
            s["n"].append(len(p))
    out["daily"] = {"days": days, "series": dstats}

    # price by hour of day (interval start), last 30 days
    pcut = (last_dt - timedelta(days=PROFILE_DAYS)).strftime(TS_FMT)
    prof: dict = defaultdict(list)
    for ts, reg, rrp, *_ in rows:
        if ts > pcut:
            h = (datetime.strptime(ts, TS_FMT) - timedelta(minutes=5)).hour
            prof[(reg, h)].append(rrp)
    out["profile"] = {"days": PROFILE_DAYS, "hours": list(range(24)), "series": {
        reg: {"avg": [_r(_avg(prof[(reg, h)])) for h in range(24)],
              "neg_pct": [_r(100 * sum(1 for x in prof[(reg, h)] if x < 0) / len(prof[(reg, h)]), 1)
                          if prof[(reg, h)] else None for h in range(24)]}
        for reg in REGIONS}}

    # negative-price share by month x hour of day
    heat: dict = defaultdict(lambda: [0, 0])
    for ts, reg, rrp, *_ in rows:
        start = datetime.strptime(ts, TS_FMT) - timedelta(minutes=5)
        c = heat[(reg, start.strftime("%Y-%m"), start.hour)]
        c[0] += rrp < 0
        c[1] += 1
    months = sorted({k[1] for k in heat})
    out["heatmap"] = {"months": months, "hours": list(range(24)), "series": {
        reg: [[_r(100 * heat[(reg, m, h)][0] / heat[(reg, m, h)][1], 1) if heat[(reg, m, h)][1] else None
               for h in range(24)] for m in months]
        for reg in REGIONS}}

    # interconnector flows, last 48 h
    ic_rows = []
    for day in store.days("interconnectors"):
        if day < nem_day(cut):
            continue
        for r in store.read_day("interconnectors", day):
            if r["settlementdate"] > cut:
                ic_rows.append((r["settlementdate"], r["interconnectorid"], _num(r["meteredmwflow"])))
    ic_ids = sorted({r[1] for r in ic_rows})
    ic_flow = {i: [None] * len(axis) for i in ic_ids}
    for ts, ic, flow in ic_rows:
        if ts in pos:
            ic_flow[ic][pos[ts]] = _r(flow, 0)
    out["interconnectors"] = {"ids": ic_ids, "flow": ic_flow}
    return out


def write_dashboard(store: Store, out_path: Path | str) -> dict:
    data = build(store)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, separators=(",", ":")))
    return data
