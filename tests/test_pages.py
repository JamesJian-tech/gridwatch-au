import io
import zipfile
from datetime import date, datetime, timedelta

import pytest

from nemdoris import aemo, pages
from test_nemdoris import DISPATCHIS_CSV


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.CSV", text)
    return buf.getvalue()


def _dispatchis(intervals, price_fn, regions=pages.REGIONS) -> bytes:
    """Synthetic DispatchIS zip: one PRICE + REGIONSUM row per (interval end, region)."""
    lines = [
        "I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,INTERVENTION,RRP",
        "I,DISPATCH,REGIONSUM,9,SETTLEMENTDATE,RUNNO,REGIONID,INTERVENTION,TOTALDEMAND,SS_SOLAR_CLEAREDMW,SS_WIND_CLEAREDMW",
        "I,DISPATCH,INTERCONNECTORRES,3,SETTLEMENTDATE,RUNNO,INTERCONNECTORID,INTERVENTION,METEREDMWFLOW",
    ]
    for t in intervals:
        s = t.strftime("%Y/%m/%d %H:%M:%S")
        for reg in regions:
            lines.append(f'D,DISPATCH,PRICE,5,"{s}",1,{reg},0,{price_fn(t, reg)}')
            lines.append(f'D,DISPATCH,PRICE,5,"{s}",1,{reg},1,99999')  # intervention run: ignored
            lines.append(f'D,DISPATCH,REGIONSUM,9,"{s}",1,{reg},0,1000,100,100')
        lines.append(f'D,DISPATCH,INTERCONNECTORRES,3,"{s}",1,VIC1-NSW1,0,500')
    return _zip("\n".join(lines) + "\n")


def _day_intervals(d: date):
    start = datetime(d.year, d.month, d.day) + timedelta(minutes=5)
    return [start + timedelta(minutes=5 * i) for i in range(pages.INTERVALS_PER_DAY)]


def test_nem_day_uses_interval_start():
    assert pages.nem_day("2026-11-12 00:00:00") == "2026-11-11"
    assert pages.nem_day("2026-11-12 00:05:00") == "2026-11-12"


def test_parse_since():
    today = date(2026, 11, 12)
    assert pages.parse_since("2026-10-01", today) == date(2026, 10, 1)
    assert pages.parse_since(" 30 ", today) == date(2026, 10, 13)
    with pytest.raises(ValueError):
        pages.parse_since("3 days", today)


def test_extract_joins_price_and_regionsum():
    regions, ics = pages.extract(_zip(DISPATCHIS_CSV))
    by = {r["regionid"]: r for r in regions}
    assert set(by) == {"NSW1", "VIC1"}  # row without region dropped
    assert by["VIC1"]["rrp"] == -12.5 and by["VIC1"]["totaldemand"] == 5123.4
    assert by["NSW1"]["totaldemand"] is None  # no REGIONSUM row for NSW1
    assert ics == [{"settlementdate": "2026-11-11 22:20:00", "interconnectorid": "VIC1-NSW1",
                    "meteredmwflow": 612.3, "exportlimit": 1100.0, "importlimit": -900.0}]


def test_store_upsert_dedupes_and_newer_wins(tmp_path):
    store = pages.Store(tmp_path)
    data = _dispatchis(_day_intervals(date(2026, 11, 10))[:3], lambda t, r: 50)
    store.ingest(data)
    store.ingest(data)
    rows = store.read_day("regions", "2026-11-10")
    assert len(rows) == 3 * 5
    store.ingest(_dispatchis(_day_intervals(date(2026, 11, 10))[:1], lambda t, r: -7.25))
    rows = store.read_day("regions", "2026-11-10")
    assert len(rows) == 15
    assert sorted({r["rrp"] for r in rows}) == ["-7.25", "50"]
    assert not store.day_complete("2026-11-10")


def test_midnight_interval_belongs_to_previous_day(tmp_path):
    store = pages.Store(tmp_path)
    store.ingest(_dispatchis(_day_intervals(date(2026, 11, 10)), lambda t, r: 1))
    assert store.days("regions") == ["2026-11-10"]
    assert store.day_complete("2026-11-10")


def test_build_dashboard(tmp_path):
    store = pages.Store(tmp_path)
    # negative price 10:00-13:00 (interval start), 100 otherwise; SA always 400
    def price(t, reg):
        if reg == "SA1":
            return 400
        h = (t - timedelta(minutes=5)).hour
        return -20 if 10 <= h < 13 else 100
    for d in (date(2026, 11, 9), date(2026, 11, 10)):
        store.ingest(_dispatchis(_day_intervals(d), price))

    out = pages.build(store)
    assert out["last_interval"] == "2026-11-11 00:00"
    assert out["latest"]["SA1"]["rrp"] == 400
    assert len(out["recent"]["t"]) == 2 * 288  # 48 h of 5-min intervals
    assert out["recent"]["series"]["VIC1"]["rrp"][0] == 100

    daily = out["daily"]
    assert daily["days"] == ["2026-11-09", "2026-11-10"]
    vic = daily["series"]["VIC1"]
    assert vic["n"] == [288, 288]
    assert vic["neg_pct"] == [12.5, 12.5]  # 3 of 24 hours
    assert vic["avg"][0] == pytest.approx((21 * 100 - 3 * 20) / 24, abs=0.01)
    assert daily["series"]["SA1"]["n_300"] == [288, 288]
    assert vic["renew_pct"] == [20.0, 20.0]  # (100 + 100) / 1000

    prof = out["profile"]["series"]["VIC1"]
    assert prof["avg"][11] == -20 and prof["avg"][9] == 100
    assert prof["neg_pct"][12] == 100 and prof["neg_pct"][13] == 0

    heat = out["heatmap"]
    assert heat["months"] == ["2026-11"]
    assert heat["series"]["VIC1"][0][10] == 100 and heat["series"]["VIC1"][0][0] == 0

    assert out["interconnectors"]["ids"] == ["VIC1-NSW1"]
    assert out["interconnectors"]["flow"]["VIC1-NSW1"][-1] == 500


def test_build_empty_store(tmp_path):
    assert pages.build(pages.Store(tmp_path))["empty"] is True


def test_update_advances_state_and_stops_on_failure(tmp_path, monkeypatch):
    store = pages.Store(tmp_path)
    ivs = _day_intervals(date(2026, 11, 10))[:3]
    names = [(f"PUBLIC_DISPATCHIS_{t:%Y%m%d%H%M}_1.zip", t) for t in ivs]
    fail = {names[2][0]}

    def fake_download(_s, _d, name):
        if name in fail:
            raise OSError("boom")
        t = next(ts for n, ts in names if n == name)
        return _dispatchis([t], lambda *_: 10)

    monkeypatch.setattr(aemo, "fetch_listing", lambda *a: names)
    monkeypatch.setattr(aemo, "download", fake_download)
    monkeypatch.setattr(aemo, "nem_now", lambda: datetime(2026, 11, 10, 1, 0))

    store.save_state({"last_file_ts": "2026-11-10 00:00:00"})  # not a first run: no Archive/ pull
    stats = pages.update(store, None, pause=0)
    assert stats == {"archive_files": 0, "current_files": 2, "skipped_complete": 0, "failed": 1}
    assert store.load_state()["last_file_ts"] == ivs[1].strftime(pages.TS_FMT)

    fail.clear()  # next run retries only the failed file
    stats = pages.update(store, None, pause=0)
    assert stats["current_files"] == 1
    assert len(store.read_day("regions", "2026-11-10")) == 15


def test_first_run_uses_archive_and_skips_complete_days(tmp_path, monkeypatch):
    store = pages.Store(tmp_path)
    day = date(2026, 11, 10)
    ivs = _day_intervals(day)
    current = [(f"PUBLIC_DISPATCHIS_{t:%Y%m%d%H%M}_1.zip", t) for t in ivs]
    archive = [("PUBLIC_DISPATCHIS_20261110.zip", datetime(2026, 11, 10))]
    downloads = []

    def fake_listing(_s, directory, _re):
        return archive if "Archive" in directory else current

    def fake_download(_s, directory, name):
        downloads.append(name)
        assert "Archive" in directory, "complete day must not be fetched from Current/"
        return _dispatchis(ivs, lambda *_: 10)

    monkeypatch.setattr(aemo, "fetch_listing", fake_listing)
    monkeypatch.setattr(aemo, "download", fake_download)
    monkeypatch.setattr(aemo, "nem_now", lambda: datetime(2026, 11, 11, 0, 30))

    stats = pages.update(store, None, pause=0)
    assert downloads == ["PUBLIC_DISPATCHIS_20261110.zip"]
    assert stats == {"archive_files": 1, "current_files": 0, "skipped_complete": 288, "failed": 0}
    assert store.load_state()["last_file_ts"] == "2026-11-11 00:00:00"
