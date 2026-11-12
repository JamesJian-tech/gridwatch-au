"""Load generator metadata (DUID -> station, region, fuel) into duid_info.

Source: AEMO's "NEM Registration and Exemption List" spreadsheet (download it from
aemo.com.au and pass the .xlsx path). The sheet with generators has a "DUID" column
plus region / fuel / technology / capacity columns; header names have shifted over
the years, so columns are matched loosely. A plain CSV with a "duid" column also works
(e.g. your own hand-maintained mapping with a fuel_group column).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

REGIONS = {"NSW", "QLD", "VIC", "SA", "TAS"}

# target column -> list of normalised header fragments to look for (first match wins)
HEADER_MAP = {
    "station_name": ["stationname", "station"],
    "participant": ["participant"],
    "region": ["region"],
    "dispatch_type": ["dispatchtype"],
    "classification": ["classification"],
    "fuel_source_primary": ["fuelsourceprimary"],
    "fuel_source_descriptor": ["fuelsourcedescriptor"],
    "technology_type_primary": ["technologytypeprimary"],
    "technology_type_descriptor": ["technologytypedescriptor"],
    "reg_cap_mw": ["regcap", "registeredcapacity"],
    "max_cap_mw": ["maxcap", "maximumcapacity"],
    "fuel_group": ["fuelgroup"],
}
NUMERIC = {"reg_cap_mw", "max_cap_mw"}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def classify_fuel(*texts: str) -> str:
    """Collapse AEMO fuel/technology descriptions into a handful of groups."""
    t = " ".join(x for x in texts if x).lower()
    if not t:
        return "Unknown"
    if "battery" in t or "bidirectional" in t:
        return "Battery"
    if "solar" in t or "photovoltaic" in t:
        return "Solar"
    if "wind" in t:
        return "Wind"
    if "hydro" in t or "water" in t:
        return "Hydro"
    if "brown coal" in t:
        return "Brown coal"
    if "coal" in t:
        return "Black coal"
    if "gas" in t or "methane" in t:
        return "Gas"
    if "diesel" in t or "liquid" in t or "distillate" in t or "oil" in t:
        return "Liquid fuel"
    if "biomass" in t or "bagasse" in t or "waste" in t or "landfill" in t:
        return "Bioenergy"
    return "Other"


def normalise_region(r) -> str | None:
    if r is None:
        return None
    r = str(r).strip().upper()
    if not r:
        return None
    return r + "1" if r in REGIONS else r


def _map_headers(header_row) -> dict[str, int]:
    normed = [_norm(h) for h in header_row]
    mapping: dict[str, int] = {}
    if "duid" not in normed:
        return mapping
    mapping["duid"] = normed.index("duid")
    for target, frags in HEADER_MAP.items():
        for frag in frags:
            idx = next((i for i, h in enumerate(normed) if h.startswith(frag)), None)
            if idx is None:
                idx = next((i for i, h in enumerate(normed) if frag in h), None)
            if idx is not None:
                mapping[target] = idx
                break
    return mapping


def _records_from_table(rows) -> list[dict]:
    rows = iter(rows)
    mapping = None
    for _ in range(10):  # header is within the first few rows
        try:
            hdr = next(rows)
        except StopIteration:
            return []
        mapping = _map_headers(hdr)
        if mapping:
            break
    if not mapping:
        return []

    by_duid: dict[str, dict] = {}
    for row in rows:
        def cell(name):
            i = mapping.get(name)
            return row[i] if i is not None and i < len(row) else None

        duid = cell("duid")
        if duid is None or str(duid).strip() in ("", "-"):
            continue
        duid = str(duid).strip().upper()
        rec = {"duid": duid}
        for target in HEADER_MAP:
            v = cell(target)
            if target in NUMERIC:
                try:
                    v = float(v) if v not in (None, "", "-") else None
                except (TypeError, ValueError):
                    v = None
            elif v is not None:
                v = str(v).strip() or None
            rec[target] = v
        rec["region"] = normalise_region(rec.get("region"))
        if not rec.get("fuel_group"):
            rec["fuel_group"] = classify_fuel(
                rec.get("technology_type_primary"), rec.get("technology_type_descriptor"),
                rec.get("fuel_source_primary"), rec.get("fuel_source_descriptor"),
            )

        prev = by_duid.get(duid)
        if prev is None:
            by_duid[duid] = rec
        else:  # several physical units under one DUID: keep first text, largest capacity
            for k, v in rec.items():
                if prev.get(k) is None:
                    prev[k] = v
            for k in NUMERIC:
                if rec.get(k) is not None and (prev.get(k) or 0) < rec[k]:
                    prev[k] = rec[k]
    return list(by_duid.values())


def read_duid_file(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            return _records_from_table(csv.reader(f))

    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    best: list[dict] = []
    for ws in wb.worksheets:
        recs = _records_from_table(ws.iter_rows(values_only=True))
        # prefer the sheet with fuel info and the most DUIDs (the generators sheet)
        score = (any(r.get("fuel_source_primary") for r in recs), len(recs))
        if score > (any(r.get("fuel_source_primary") for r in best), len(best)):
            best = recs
    wb.close()
    return best
