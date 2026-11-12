"""Column specs for the AEMO MMS tables we load, and value conversion.

Every AEMO CSV row is keyed by (package, table) in its "I" header row, e.g.
("DISPATCH", "PRICE"). We map each of those to a Doris table and list the
columns we keep with a simple type tag. Columns AEMO adds later are ignored;
columns AEMO drops simply arrive as NULL — the parser is header-driven.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# type tags: dt = AEMO datetime "YYYY/MM/DD HH:MM:SS", str, int, num
DT, STR, INT, NUM = "dt", "str", "int", "num"

_FCAS = [
    "raise6secrrp", "raise60secrrp", "raise5minrrp", "raiseregrrp",
    "lower6secrrp", "lower60secrrp", "lower5minrrp", "lowerregrrp",
    "raise1secrrp", "lower1secrrp",
]


@dataclass(frozen=True)
class TableSpec:
    doris_table: str
    columns: dict  # column name -> type tag (insertion order = Doris column order)
    required: tuple = field(default_factory=tuple)  # key columns; row dropped if any is empty


MMS_TABLES: dict[tuple[str, str], TableSpec] = {
    ("DISPATCH", "PRICE"): TableSpec(
        "dispatch_price",
        {
            "settlementdate": DT, "regionid": STR, "intervention": INT,
            "rrp": NUM, "rop": NUM, "apcflag": INT, "marketsuspendedflag": INT,
            **{c: NUM for c in _FCAS},
            "price_status": STR, "lastchanged": DT,
        },
        required=("settlementdate", "regionid", "intervention"),
    ),
    ("DISPATCH", "REGIONSUM"): TableSpec(
        "dispatch_regionsum",
        {
            "settlementdate": DT, "regionid": STR, "intervention": INT,
            "totaldemand": NUM, "availablegeneration": NUM, "availableload": NUM,
            "demandforecast": NUM, "dispatchablegeneration": NUM, "dispatchableload": NUM,
            "netinterchange": NUM, "excessgeneration": NUM, "clearedsupply": NUM,
            "totalintermittentgeneration": NUM, "demand_and_nonschedgen": NUM, "uigf": NUM,
            "semischedule_clearedmw": NUM, "semischedule_compliancemw": NUM,
            "ss_solar_uigf": NUM, "ss_wind_uigf": NUM,
            "ss_solar_clearedmw": NUM, "ss_wind_clearedmw": NUM,
            "bdu_energy_storage": NUM, "bdu_clearedmw_gen": NUM, "bdu_clearedmw_load": NUM,
            "lastchanged": DT,
        },
        required=("settlementdate", "regionid", "intervention"),
    ),
    ("DISPATCH", "INTERCONNECTORRES"): TableSpec(
        "dispatch_interconnectorres",
        {
            "settlementdate": DT, "interconnectorid": STR, "intervention": INT,
            "meteredmwflow": NUM, "mwflow": NUM, "mwlosses": NUM, "marginalvalue": NUM,
            "exportlimit": NUM, "importlimit": NUM, "marginalloss": NUM, "lastchanged": DT,
        },
        required=("settlementdate", "interconnectorid", "intervention"),
    ),
    ("DISPATCH", "UNIT_SCADA"): TableSpec(
        "unit_scada",
        {"settlementdate": DT, "duid": STR, "scadavalue": NUM, "lastchanged": DT},
        required=("settlementdate", "duid"),
    ),
}


def convert(value: str, tag: str):
    """Convert one raw AEMO CSV field. Empty -> None. Raises ValueError on garbage."""
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    if tag == DT:
        # AEMO: 2026/11/11 22:20:00  -> Doris: 2026-11-11 22:20:00
        date, _, time = v.partition(" ")
        y, m, d = date.split("/")
        if not time:
            time = "00:00:00"
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d} {time}"
    if tag == INT:
        return int(float(v))
    if tag == NUM:
        return float(v)
    return v


def to_doris_rows(spec: TableSpec, raw_rows: list[dict]) -> tuple[list[dict], int]:
    """Project + convert raw rows. Returns (rows, n_dropped)."""
    out, dropped = [], 0
    for raw in raw_rows:
        try:
            row = {col: convert(raw.get(col), tag) for col, tag in spec.columns.items()}
        except (ValueError, TypeError):
            dropped += 1
            continue
        if any(row.get(k) is None for k in spec.required):
            dropped += 1
            continue
        out.append(row)
    return out, dropped
