"""CLI:  python -m nemdoris {check,live,backfill,load-duid,pages}

Config comes from environment variables (see .env.example):
  DORIS_HOST, DORIS_HTTP_PORT, DORIS_QUERY_PORT, DORIS_USER, DORIS_PASSWORD, DORIS_DB
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import aemo
from .doris import DorisConfig, DorisQuery, StreamLoader, StreamLoadError, make_label
from .duid import read_duid_file
from .schema import MMS_TABLES, to_doris_rows

log = logging.getLogger("nemdoris")
CHUNK_ROWS = 200_000
_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("signal %s received, finishing current file then exiting", signum)


def config_from_env() -> DorisConfig:
    return DorisConfig(
        fe_host=os.getenv("DORIS_HOST", "127.0.0.1"),
        http_port=int(os.getenv("DORIS_HTTP_PORT", "8030")),
        query_port=int(os.getenv("DORIS_QUERY_PORT", "9030")),
        user=os.getenv("DORIS_USER", "root"),
        password=os.getenv("DORIS_PASSWORD", ""),
        database=os.getenv("DORIS_DB", "nem"),
    )


class Collector:
    def __init__(self, cfg: DorisConfig):
        self.session = aemo.make_session()
        self.loader = StreamLoader(cfg)
        self.query = DorisQuery(cfg)

    def ingest_bytes(self, source: str, name: str, file_ts: datetime, data: bytes) -> int:
        """Parse one NEMWEB zip and stream-load every table we know. Returns rows loaded."""
        tables = aemo.parse_zip(data)
        total = 0
        for key, spec in MMS_TABLES.items():
            raw = tables.get(key)
            if not raw:
                continue
            rows, dropped = to_doris_rows(spec, raw)
            if dropped:
                log.warning("%s %s: dropped %d malformed rows", name, spec.doris_table, dropped)
            for i in range(0, len(rows), CHUNK_ROWS):
                part = rows[i:i + CHUNK_ROWS]
                label = make_label(spec.doris_table, f"{name}#{i // CHUNK_ROWS}")
                self.loader.load(spec.doris_table, part, label)
            total += len(rows)

        self.loader.load(
            "ingest_log",
            [{
                "source": source, "file_name": name,
                "file_ts": file_ts.strftime("%Y-%m-%d %H:%M:%S"), "n_rows": total,
                "loaded_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }],
            make_label("ingest_log", name),
        )
        return total

    def process(self, src: aemo.Source, directory: str, name: str, ts: datetime) -> bool:
        try:
            data = aemo.download(self.session, directory, name)
            n = self.ingest_bytes(src.name, name, ts, data)
            log.info("loaded %-55s %7d rows", name, n)
            return True
        except (StreamLoadError, OSError, ValueError) as e:
            log.error("failed %s: %s", name, e)
            return False
        except Exception as e:  # keep the daemon alive on anything unexpected
            log.exception("unexpected error on %s: %s", name, e)
            return False

    # ---- commands -------------------------------------------------------

    def live_pass(self, sources, lookback_hours: float, max_files: int | None) -> int:
        done = 0
        for src in sources:
            listing = aemo.fetch_listing(self.session, src.current_dir, src.current_re)
            since = aemo.nem_now() - timedelta(hours=lookback_hours)
            loaded = self.query.loaded_files(src.name, since)
            todo = [(n, ts) for n, ts in listing if ts >= since and n not in loaded]
            if max_files:
                todo = todo[-max_files:]
            if todo:
                log.info("%s: %d new file(s)", src.name, len(todo))
            for name, ts in todo:
                if _stop:
                    return done
                done += self.process(src, src.current_dir, name, ts)
        return done

    def backfill(self, sources, start: datetime, end: datetime) -> int:
        done = 0
        for src in sources:
            listing = aemo.fetch_listing(self.session, src.archive_dir, src.archive_re)
            todo = [(n, ts) for n, ts in listing if start <= ts <= end]
            loaded = self.query.loaded_files(src.name, start)
            todo = [(n, ts) for n, ts in todo if n not in loaded]
            avail = f"{listing[0][1]:%Y-%m-%d}..{listing[-1][1]:%Y-%m-%d}" if listing else "none"
            log.info("%s archive: %d day(s) to load (archive covers %s)", src.name, len(todo), avail)
            for name, ts in todo:
                if _stop:
                    return done
                done += self.process(src, src.archive_dir, name, ts)
                time.sleep(1)  # be polite to NEMWEB
        return done


def _sources(arg: str):
    names = [s.strip() for s in arg.split(",") if s.strip()]
    bad = [n for n in names if n not in aemo.SOURCES]
    if bad:
        sys.exit(f"unknown source(s): {bad}; choose from {list(aemo.SOURCES)}")
    return [aemo.SOURCES[n] for n in names]


def cmd_check(cfg: DorisConfig, args) -> int:
    ok = True
    s = aemo.make_session()
    for src in aemo.SOURCES.values():
        try:
            cur = aemo.fetch_listing(s, src.current_dir, src.current_re)
            print(f"[ok] NEMWEB {src.name}: {len(cur)} current files, latest {cur[-1][0] if cur else '-'}")
        except Exception as e:
            ok = False
            print(f"[!!] NEMWEB {src.name}: {e}")
    try:
        q = DorisQuery(cfg)
        print(f"[ok] Doris {q.query('SELECT VERSION()')[0][0]}")
        for t in ["dispatch_price", "dispatch_regionsum", "dispatch_interconnectorres",
                  "unit_scada", "duid_info", "ingest_log"]:
            n, lo, hi = (q.query(f"SELECT COUNT(*), NULL, NULL FROM {t}")[0] if t in ("duid_info", "ingest_log")
                         else q.query(f"SELECT COUNT(*), MIN(settlementdate), MAX(settlementdate) FROM {t}")[0])
            span = f"  {lo} .. {hi}" if lo else ""
            print(f"     {t:28s} {n:>12,}{span}")
    except Exception as e:
        ok = False
        print(f"[!!] Doris: {e}")
    return 0 if ok else 1


def cmd_live(cfg, args) -> int:
    c = Collector(cfg)
    sources = _sources(args.sources)
    while not _stop:
        try:
            c.live_pass(sources, args.lookback_hours, args.max_files)
        except Exception as e:
            log.error("poll failed: %s", e)
            if args.once:
                return 1
        if args.once:
            break
        for _ in range(args.interval):
            if _stop:
                break
            time.sleep(1)
    return 0


def cmd_backfill(cfg, args) -> int:
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else aemo.nem_now()
    n = Collector(cfg).backfill(_sources(args.sources), start, end)
    log.info("backfill finished: %d file(s) loaded", n)
    return 0


def cmd_load_duid(cfg, args) -> int:
    path = Path(args.path)
    recs = read_duid_file(path)
    if not recs:
        sys.exit(f"no DUID rows found in {path} (need a sheet/CSV with a 'DUID' column)")
    label = make_label("duid_info", f"{path.resolve()}|{path.stat().st_mtime_ns}")
    StreamLoader(cfg).load("duid_info", recs, label)
    groups: dict[str, int] = {}
    for r in recs:
        groups[r["fuel_group"]] = groups.get(r["fuel_group"], 0) + 1
    print(f"loaded {len(recs)} DUIDs: " + ", ".join(f"{k} {v}" for k, v in sorted(groups.items())))
    return 0


def cmd_pages(_cfg, args) -> int:
    from . import pages

    store = pages.Store(args.store)
    since = pages.parse_since(args.backfill_from, aemo.nem_now().date()) if args.backfill_from else None
    try:
        stats = pages.update(store, aemo.make_session(), since=since, max_files=args.max_files)
        log.info("store updated: %s", stats)
    except Exception as e:  # NEMWEB down: still rebuild the page from what the store has
        log.error("NEMWEB update failed, building from existing store: %s", e)
    data = pages.write_dashboard(store, args.out)
    if data.get("empty"):
        log.error("store is empty; nothing to show")
        return 1
    log.info("wrote %s (last interval %s, %d day(s))", args.out, data["last_interval"], len(data["daily"]["days"]))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="nemdoris", description="AEMO NEMWEB -> Apache Doris")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="test NEMWEB + Doris connectivity and show table sizes")

    lv = sub.add_parser("live", help="poll NEMWEB Current/ and load new 5-minute files")
    lv.add_argument("--sources", default="dispatchis,scada")
    lv.add_argument("--interval", type=int, default=60, help="seconds between polls")
    lv.add_argument("--lookback-hours", type=float, default=48,
                    help="only consider files newer than this (Current/ keeps ~2 days)")
    lv.add_argument("--max-files", type=int, default=None, help="cap files per source per poll")
    lv.add_argument("--once", action="store_true", help="single pass then exit (for cron)")

    bf = sub.add_parser("backfill", help="load daily files from NEMWEB Archive/ (~13 months)")
    bf.add_argument("--start", required=True, help="YYYY-MM-DD (NEM date)")
    bf.add_argument("--end", help="YYYY-MM-DD, default today")
    bf.add_argument("--sources", default="dispatchis,scada")

    ld = sub.add_parser("load-duid", help="load AEMO registration list (.xlsx) or a CSV into duid_info")
    ld.add_argument("path")

    pg = sub.add_parser("pages", help="update the static-site CSV store from NEMWEB and write dashboard JSON (no Doris)")
    pg.add_argument("--store", required=True, help="store directory (the site-data branch checkout)")
    pg.add_argument("--out", required=True, help="dashboard JSON path, e.g. _site/data/dashboard.json")
    pg.add_argument("--backfill-from", help="also load Archive/ days from YYYY-MM-DD, or N days ago")
    pg.add_argument("--max-files", type=int, default=None, help="cap new Current/ files (local preview)")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    cfg = config_from_env()
    return {"check": cmd_check, "live": cmd_live, "backfill": cmd_backfill,
            "load-duid": cmd_load_duid, "pages": cmd_pages}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
