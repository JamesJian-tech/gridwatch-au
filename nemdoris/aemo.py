"""NEMWEB access and AEMO MMS CSV parsing.

NEMWEB layout (verified Nov 2026):
  /Reports/Current/DispatchIS_Reports/PUBLIC_DISPATCHIS_YYYYMMDDHHMM_<seq>.zip   (~2 days, every 5 min)
  /Reports/Archive/DispatchIS_Reports/PUBLIC_DISPATCHIS_YYYYMMDD.zip             (~13 months, daily, zip of zips)
  /Reports/Current/Dispatch_SCADA/PUBLIC_DISPATCHSCADA_YYYYMMDDHHMM_<seq>.zip
  /Reports/Archive/Dispatch_SCADA/PUBLIC_DISPATCHSCADA_YYYYMMDD.zip

All timestamps in file names and data are NEM time = AEST (UTC+10), no daylight saving.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

NEMWEB = "https://nemweb.com.au"
NEM_TZ = timezone(timedelta(hours=10), "AEST")
USER_AGENT = "nem-doris-collector/1.0 (personal research; polite 60s polling)"


@dataclass(frozen=True)
class Source:
    name: str
    current_dir: str
    archive_dir: str
    current_re: re.Pattern
    archive_re: re.Pattern


SOURCES = {
    "dispatchis": Source(
        "dispatchis",
        "/Reports/Current/DispatchIS_Reports/",
        "/Reports/Archive/DispatchIS_Reports/",
        re.compile(r"PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip", re.I),
        re.compile(r"PUBLIC_DISPATCHIS_(\d{8})\.zip", re.I),
    ),
    "scada": Source(
        "scada",
        "/Reports/Current/Dispatch_SCADA/",
        "/Reports/Archive/Dispatch_SCADA/",
        re.compile(r"PUBLIC_DISPATCHSCADA_(\d{12})_\d+\.zip", re.I),
        re.compile(r"PUBLIC_DISPATCHSCADA_(\d{8})\.zip", re.I),
    ),
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def list_files(html: str, pattern: re.Pattern) -> list[tuple[str, datetime]]:
    """Extract (file_name, nem_timestamp) pairs from a NEMWEB directory listing, oldest first."""
    found: dict[str, datetime] = {}
    for m in pattern.finditer(html):
        stamp = m.group(1)
        fmt = "%Y%m%d%H%M" if len(stamp) == 12 else "%Y%m%d"
        found[m.group(0)] = datetime.strptime(stamp, fmt)
    return sorted(found.items(), key=lambda kv: (kv[1], kv[0]))


def fetch_listing(session: requests.Session, directory: str, pattern: re.Pattern):
    r = session.get(NEMWEB + directory, timeout=60)
    r.raise_for_status()
    return list_files(r.text, pattern)


def download(session: requests.Session, directory: str, name: str) -> bytes:
    r = session.get(NEMWEB + directory + name, timeout=180)
    r.raise_for_status()
    return r.content


def iter_csv_texts(data: bytes):
    """Yield the text of every CSV inside zip bytes, recursing into nested zips."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            lower = info.filename.lower()
            payload = zf.read(info)
            if lower.endswith(".zip"):
                yield from iter_csv_texts(payload)
            elif lower.endswith(".csv"):
                yield payload.decode("utf-8", errors="replace")


def parse_mms_csv(text: str) -> dict[tuple[str, str], list[dict]]:
    """Parse an AEMO MMS multi-table CSV.

    C rows are comments, I rows are headers (I,PACKAGE,TABLE,VERSION,col1,col2...),
    D rows are data aligned with the most recent I row for the same (package, table).
    """
    headers: dict[tuple[str, str], list[str]] = {}
    tables: dict[tuple[str, str], list[dict]] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5:
            continue
        kind = row[0].strip().upper()
        key = (row[1].strip().upper(), row[2].strip().upper())
        if kind == "I":
            headers[key] = [c.strip().lower() for c in row[4:]]
            tables.setdefault(key, [])
        elif kind == "D":
            cols = headers.get(key)
            if cols is not None:
                tables[key].append(dict(zip(cols, row[4:])))
    return tables


def parse_zip(data: bytes) -> dict[tuple[str, str], list[dict]]:
    """Parse every CSV in a (possibly nested) zip and merge rows per table."""
    merged: dict[tuple[str, str], list[dict]] = {}
    for text in iter_csv_texts(data):
        for key, rows in parse_mms_csv(text).items():
            merged.setdefault(key, []).extend(rows)
    return merged


def nem_now() -> datetime:
    return datetime.now(NEM_TZ).replace(tzinfo=None)
