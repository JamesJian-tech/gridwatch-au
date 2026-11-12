import base64
import io
import json
import threading
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nemdoris import aemo
from nemdoris.__main__ import Collector
from nemdoris.doris import DorisConfig, StreamLoader, StreamLoadError, make_label
from nemdoris.duid import classify_fuel, normalise_region, read_duid_file
from nemdoris.schema import MMS_TABLES, convert, to_doris_rows

DISPATCHIS_CSV = """C,NEMP.WORLD,DISPATCHIS,AEMO,PUBLIC,2026/11/11,22:15:12,0000000548059319,DISPATCHIS,0000000548059319
I,DISPATCH,CASE_SOLUTION,2,SETTLEMENTDATE,RUNNO,INTERVENTION,CASESUBTYPE,SOLUTIONSTATUS
D,DISPATCH,CASE_SOLUTION,2,"2026/11/11 22:20:00",1,0,,0
I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,RRP,EEP,ROP,APCFLAG,MARKETSUSPENDEDFLAG,LASTCHANGED,RAISE6SECRRP,LOWER6SECRRP,RAISE1SECRRP,PRICE_STATUS,SOME_NEW_COLUMN
D,DISPATCH,PRICE,5,"2026/11/11 22:20:00",1,NSW1,20261111269,0,95.12,0,95.12,0,0,"2026/11/11 22:15:10",0.5,0.1,0.9,FIRM,xyz
D,DISPATCH,PRICE,5,"2026/11/11 22:20:00",1,VIC1,20261111269,0,-12.5,0,-12.5,0,0,"2026/11/11 22:15:10",,0.1,,FIRM,xyz
D,DISPATCH,PRICE,5,"2026/11/11 22:20:00",1,,20261111269,0,1,0,1,0,0,"2026/11/11 22:15:10",,,,FIRM,bad-row-no-region
I,DISPATCH,REGIONSUM,9,SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,TOTALDEMAND,AVAILABLEGENERATION,NETINTERCHANGE,SS_SOLAR_CLEAREDMW,LASTCHANGED
D,DISPATCH,REGIONSUM,9,"2026/11/11 22:20:00",1,VIC1,20261111269,0,5123.4,9000,-150.2,0,"2026/11/11 22:15:10"
I,DISPATCH,INTERCONNECTORRES,3,SETTLEMENTDATE,RUNNO,INTERCONNECTORID,DISPATCHINTERVAL,INTERVENTION,METEREDMWFLOW,MWFLOW,MWLOSSES,MARGINALVALUE,VIOLATIONDEGREE,LASTCHANGED,EXPORTLIMIT,IMPORTLIMIT
D,DISPATCH,INTERCONNECTORRES,3,"2026/11/11 22:20:00",1,VIC1-NSW1,20261111269,0,612.3,600,20.1,0,0,"2026/11/11 22:15:10",1100,-900
C,"END OF REPORT",12
"""

SCADA_CSV = """C,NEMP.WORLD,DISPATCHSCADA,AEMO,PUBLIC,2026/11/11,22:15:12,0000000548059324,DISPATCHSCADA,0000000548059324
I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE,LASTCHANGED
D,DISPATCH,UNIT_SCADA,1,"2026/11/11 22:20:00",LYA1,520.12,"2026/11/11 22:15:10"
D,DISPATCH,UNIT_SCADA,1,"2026/11/11 22:20:00",VBB1,-150.5,"2026/11/11 22:15:10"
C,"END OF REPORT",4
"""


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


# ---------------------------------------------------------------- parsing

def test_parse_mms_csv_tables_and_unknown_columns():
    t = aemo.parse_mms_csv(DISPATCHIS_CSV)
    assert set(t) >= {("DISPATCH", "PRICE"), ("DISPATCH", "REGIONSUM"), ("DISPATCH", "INTERCONNECTORRES")}
    assert len(t[("DISPATCH", "PRICE")]) == 3
    assert t[("DISPATCH", "PRICE")][0]["rrp"] == "95.12"
    assert t[("DISPATCH", "PRICE")][0]["some_new_column"] == "xyz"


def test_to_doris_rows_converts_and_drops_bad_rows():
    t = aemo.parse_mms_csv(DISPATCHIS_CSV)
    rows, dropped = to_doris_rows(MMS_TABLES[("DISPATCH", "PRICE")], t[("DISPATCH", "PRICE")])
    assert dropped == 1  # missing regionid
    nsw, vic = rows
    assert nsw["settlementdate"] == "2026-11-11 22:20:00"
    assert nsw["rrp"] == 95.12 and nsw["intervention"] == 0 and nsw["price_status"] == "FIRM"
    assert vic["rrp"] == -12.5 and vic["raise6secrrp"] is None
    assert nsw["raise60secrrp"] is None          # column absent from file -> NULL
    assert "some_new_column" not in nsw and "eep" not in nsw   # not in schema -> dropped
    assert list(nsw) == list(MMS_TABLES[("DISPATCH", "PRICE")].columns)


def test_convert_edge_cases():
    assert convert("2026/1/5 04:05:00", "dt") == "2026-01-05 04:05:00"
    assert convert("2026/01/05", "dt") == "2026-01-05 00:00:00"
    assert convert("  ", "num") is None
    assert convert("1.0", "int") == 1
    with pytest.raises(ValueError):
        convert("abc", "num")


def test_parse_zip_handles_nested_daily_archive():
    inner1 = _zip({"PUBLIC_DISPATCHSCADA_202611112220_1.CSV": SCADA_CSV})
    inner2 = _zip({"PUBLIC_DISPATCHSCADA_202611112225_2.CSV": SCADA_CSV.replace("22:20:00", "22:25:00")})
    daily = _zip({"PUBLIC_DISPATCHSCADA_202611112220_1.zip": inner1,
                  "PUBLIC_DISPATCHSCADA_202611112225_2.zip": inner2})
    rows = aemo.parse_zip(daily)[("DISPATCH", "UNIT_SCADA")]
    assert len(rows) == 4
    assert {r["settlementdate"] for r in rows} == {"2026/11/11 22:20:00", "2026/11/11 22:25:00"}


def test_list_files_from_listing_html():
    html = """<pre><A HREF="/Reports/CURRENT/DispatchIS_Reports/">[To Parent Directory]</A><br><br>
     Monday, October 12, 2026 09:02 PM &lt;dir&gt; <A HREF="/Reports/CURRENT/DispatchIS_Reports/DUPLICATE/">DUPLICATE</A><br>
     Wednesday, November 11, 2026 10:16 PM 19721 <A HREF="/Reports/CURRENT/DispatchIS_Reports/PUBLIC_DISPATCHIS_202611112220_0000000548059319.zip">PUBLIC_DISPATCHIS_202611112220_0000000548059319.zip</A><br>
     Wednesday, November 11, 2026 10:11 PM 19789 <A HREF="/Reports/CURRENT/DispatchIS_Reports/PUBLIC_DISPATCHIS_202611112215_0000000548058811.zip">PUBLIC_DISPATCHIS_202611112215_0000000548058811.zip</A><br></pre>"""
    files = aemo.list_files(html, aemo.SOURCES["dispatchis"].current_re)
    assert [f[0] for f in files] == [
        "PUBLIC_DISPATCHIS_202611112215_0000000548058811.zip",
        "PUBLIC_DISPATCHIS_202611112220_0000000548059319.zip",
    ]
    assert files[-1][1] == datetime(2026, 11, 11, 22, 20)

    arch = aemo.list_files('<A HREF="x/PUBLIC_DISPATCHSCADA_20261109.zip">PUBLIC_DISPATCHSCADA_20261109.zip</A>',
                           aemo.SOURCES["scada"].archive_re)
    assert arch == [("PUBLIC_DISPATCHSCADA_20261109.zip", datetime(2026, 11, 9))]


# ---------------------------------------------------------------- stream load

class _FakeDoris:
    """FE on one port that 307-redirects to a BE on another port, like real Doris."""

    def __init__(self, be_reply):
        self.received = []
        outer = self

        class BE(BaseHTTPRequestHandler):
            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.received.append((dict(self.headers), json.loads(body)))
                reply = json.dumps(be_reply(self.headers)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *a):
                pass

        self.be = HTTPServer(("127.0.0.1", 0), BE)
        be_port = self.be.server_port

        class FE(BaseHTTPRequestHandler):
            def do_PUT(self):
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{be_port}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.fe = HTTPServer(("127.0.0.1", 0), FE)
        for s in (self.be, self.fe):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def cfg(self):
        return DorisConfig(fe_host="127.0.0.1", http_port=self.fe.server_port, user="root", password="pw")

    def close(self):
        self.fe.shutdown()
        self.be.shutdown()


def test_stream_load_follows_redirect_with_auth():
    fake = _FakeDoris(lambda h: {"Status": "Success", "NumberLoadedRows": 2})
    try:
        res = StreamLoader(fake.cfg()).load("dispatch_price", [{"a": 1}, {"a": 2}], "lbl1")
        assert res["Status"] == "Success"
        headers, body = fake.received[0]
        assert headers["Authorization"] == "Basic " + base64.b64encode(b"root:pw").decode()
        assert headers["label"] == "lbl1" and headers["format"] == "json"
        assert headers["strip_outer_array"] == "true"
        assert body == [{"a": 1}, {"a": 2}]
    finally:
        fake.close()


def test_stream_load_label_exists_ok_and_failure_raises():
    fake = _FakeDoris(lambda h: {"Status": "Label Already Exists"})
    try:
        assert StreamLoader(fake.cfg()).load("t", [{"a": 1}], "x")["NumberLoadedRows"] == 0
    finally:
        fake.close()
    fake = _FakeDoris(lambda h: {"Status": "Fail", "Message": "too many filtered rows", "ErrorURL": "http://e"})
    try:
        with pytest.raises(StreamLoadError, match="too many filtered rows"):
            StreamLoader(fake.cfg()).load("t", [{"a": 1}], "x")
    finally:
        fake.close()


def test_labels_are_deterministic_and_valid():
    a = make_label("unit_scada", "PUBLIC_DISPATCHSCADA_20261109.zip#0")
    assert a == make_label("unit_scada", "PUBLIC_DISPATCHSCADA_20261109.zip#0")
    assert a != make_label("unit_scada", "PUBLIC_DISPATCHSCADA_20261109.zip#1")
    assert len(a) <= 128 and all(c.isalnum() or c in "_-" for c in a)


def test_collector_ingest_bytes_loads_every_table_then_logs():
    class RecLoader:
        def __init__(self):
            self.calls = []

        def load(self, table, rows, label):
            self.calls.append((table, rows, label))

    c = Collector.__new__(Collector)
    c.loader = RecLoader()
    data = _zip({"PUBLIC_DISPATCHIS_202611112220_1.CSV": DISPATCHIS_CSV})
    n = c.ingest_bytes("dispatchis", "PUBLIC_DISPATCHIS_202611112220_1.zip", datetime(2026, 11, 11, 22, 20), data)
    tables = [t for t, _, _ in c.loader.calls]
    assert tables == ["dispatch_price", "dispatch_regionsum", "dispatch_interconnectorres", "ingest_log"]
    assert n == 2 + 1 + 1
    log_row = c.loader.calls[-1][1][0]
    assert log_row["file_ts"] == "2026-11-11 22:20:00" and log_row["n_rows"] == 4
    ic = c.loader.calls[2][1][0]
    assert ic["interconnectorid"] == "VIC1-NSW1" and ic["importlimit"] == -900.0


# ---------------------------------------------------------------- duid metadata

def test_classify_fuel():
    assert classify_fuel("Battery Storage", "", "Battery storage", "Grid") == "Battery"
    assert classify_fuel("Combustion", "Steam Sub-Critical", "Fossil", "Brown coal") == "Brown coal"
    assert classify_fuel("Combustion", "Steam Sub-Critical", "Fossil", "Black coal") == "Black coal"
    assert classify_fuel("Combustion", "OCGT", "Fossil", "Natural Gas Pipeline") == "Gas"
    assert classify_fuel("Renewable", "Wind - Onshore", "Wind", "Wind") == "Wind"
    assert classify_fuel("Renewable", "Photovoltaic Tracking Flat panel", "Solar", "Solar") == "Solar"
    assert classify_fuel("Renewable", "Hydro - Gravity", "Hydro", "Water") == "Hydro"
    assert classify_fuel() == "Unknown"
    assert normalise_region("Vic") == "VIC1" and normalise_region("SA1") == "SA1"


def test_read_duid_xlsx(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Region Contacts"
    ws.append(["Region", "Contact"])
    ws.append(["VIC", "someone"])
    gen = wb.create_sheet("PU and Scheduled Loads")
    gen.append(["Participant", "Station Name", "Region", "Dispatch Type", "Category", "Classification",
                "Fuel Source - Primary", "Fuel Source - Descriptor", "Technology Type - Primary",
                "Technology Type - Descriptor", "Physical Unit No.", "Unit Size (MW)", "Aggregation",
                "DUID", "Reg Cap generation (MW)", "Max Cap generation (MW)"])
    gen.append(["AGL", "Loy Yang A", "VIC1", "Generator", "Market", "Scheduled", "Fossil", "Brown coal",
                "Combustion", "Steam Sub-Critical", "1", 560, "N", "LYA1", 560, 560])
    gen.append(["AGL", "Loy Yang A", "VIC1", "Generator", "Market", "Scheduled", "Fossil", "Brown coal",
                "Combustion", "Steam Sub-Critical", "2", 560, "N", "LYA1", 565, 570])
    gen.append(["Neoen", "Victorian Big Battery", "VIC1", "Bidirectional Unit", "Market", "Scheduled",
                "Battery Storage", "Grid", "Battery Storage", "Battery", "1", 300, "N", "VBB1", 300, 300])
    gen.append(["x", "y", "VIC1", "", "", "", "", "", "", "", "", "", "", "-", "", ""])
    path = tmp_path / "reg.xlsx"
    wb.save(path)

    recs = {r["duid"]: r for r in read_duid_file(path)}
    assert set(recs) == {"LYA1", "VBB1"}
    assert recs["LYA1"]["fuel_group"] == "Brown coal" and recs["LYA1"]["reg_cap_mw"] == 565
    assert recs["LYA1"]["max_cap_mw"] == 570 and recs["LYA1"]["region"] == "VIC1"
    assert recs["VBB1"]["fuel_group"] == "Battery" and recs["VBB1"]["station_name"] == "Victorian Big Battery"


def test_read_duid_csv(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("duid,region,fuel_group\nfoo1,nsw,Gas\n")
    (rec,) = read_duid_file(p)
    assert rec == {**rec, "duid": "FOO1", "region": "NSW1", "fuel_group": "Gas"}
