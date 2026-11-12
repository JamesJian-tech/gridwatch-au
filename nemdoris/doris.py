"""Doris access: Stream Load over HTTP for writes, MySQL protocol for reads."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime

import requests

log = logging.getLogger(__name__)

_OK_STATUSES = {"Success", "Publish Timeout"}  # Publish Timeout = committed, visible shortly


class StreamLoadError(RuntimeError):
    pass


@dataclass
class DorisConfig:
    fe_host: str = "127.0.0.1"
    http_port: int = 8030
    query_port: int = 9030
    user: str = "root"
    password: str = ""
    database: str = "nem"

    @property
    def fe_http(self) -> str:
        return f"http://{self.fe_host}:{self.http_port}"


def make_label(table: str, source_file: str) -> str:
    """Deterministic label so re-sending the same file is a no-op inside Doris' label window."""
    h = hashlib.sha1(f"{table}|{source_file}".encode()).hexdigest()[:20]
    return f"nem_{table}_{h}"


class StreamLoader:
    def __init__(self, cfg: DorisConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()

    def load(self, table: str, rows: list[dict], label: str) -> dict:
        if not rows:
            return {"Status": "Skipped", "NumberLoadedRows": 0}
        body = json.dumps(rows, separators=(",", ":")).encode()
        headers = {
            "Expect": "100-continue",
            "format": "json",
            "strip_outer_array": "true",
            "label": label,
            "Content-Type": "application/json",
        }
        url = f"{self.cfg.fe_http}/api/{self.cfg.database}/{table}/_stream_load"
        auth = (self.cfg.user, self.cfg.password)

        # FE answers with a 307 to a BE. requests drops the Authorization header when the
        # redirect changes port/host, so follow the redirect ourselves and re-send the body.
        for _ in range(3):
            resp = self.session.put(url, data=body, headers=headers, auth=auth,
                                    allow_redirects=False, timeout=300)
            if resp.status_code in (301, 302, 303, 307, 308) and "Location" in resp.headers:
                url = resp.headers["Location"]
                continue
            break
        else:
            raise StreamLoadError(f"too many redirects loading {table}")

        if resp.status_code != 200:
            raise StreamLoadError(f"{table}: HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            result = resp.json()
        except ValueError as e:
            raise StreamLoadError(f"{table}: non-JSON response: {resp.text[:500]}") from e

        status = result.get("Status")
        if status in _OK_STATUSES:
            return result
        if status == "Label Already Exists":
            # Already loaded (or loading) under this label — treat as success.
            log.debug("%s: label %s already exists, skipping", table, label)
            return {"Status": status, "NumberLoadedRows": 0}
        raise StreamLoadError(
            f"{table}: {status}: {result.get('Message')} (ErrorURL: {result.get('ErrorURL')})"
        )


class DorisQuery:
    """Thin read-only helper over the MySQL protocol."""

    def __init__(self, cfg: DorisConfig):
        self.cfg = cfg
        self._conn = None

    def _connect(self):
        import pymysql  # imported lazily so parsing/tests don't need it

        if self._conn is None or not self._conn.open:
            self._conn = pymysql.connect(
                host=self.cfg.fe_host, port=self.cfg.query_port, user=self.cfg.user,
                password=self.cfg.password, database=self.cfg.database,
                autocommit=True, connect_timeout=10,
            )
        return self._conn

    def query(self, sql: str, args=None) -> list[tuple]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())

    def loaded_files(self, source: str, since: datetime | None = None) -> set[str]:
        sql = "SELECT file_name FROM ingest_log WHERE source = %s"
        args: list = [source]
        if since is not None:
            sql += " AND file_ts >= %s"
            args.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        return {r[0] for r in self.query(sql, args)}
