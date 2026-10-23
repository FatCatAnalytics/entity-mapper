"""Local workspace: alias memory, run history and an audit log in one DuckDB file.

Nothing here touches the network. Default location: ~/EntityMapper/workspace.duckdb
(override with ENTITY_MAPPER_HOME).
"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import duckdb
import pandas as pd

from .matcher import NO_MATCH
from .normalize import normalize_country
from .shared import current_user, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS aliases (
    bank_code   VARCHAR,
    name_clean  VARCHAR,
    country     VARCHAR,
    inhouse_id  VARCHAR,          -- '__NO_MATCH__' = confirmed non-match
    raw_name    VARCHAR,
    decided_by  VARCHAR,
    decided_at  TIMESTAMP,
    run_id      VARCHAR,
    PRIMARY KEY (bank_code, name_clean, country)
);
CREATE TABLE IF NOT EXISTS master_map (
    name        VARCHAR,          -- bank's spelling, as it was mapped
    country     VARCHAR,
    inhouse_id  VARCHAR,
    bank_code   VARCHAR,
    source      VARCHAR,          -- file it was imported from
    imported_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, bank_code VARCHAR, started_at TIMESTAMP,
    n_rows INTEGER, n_auto INTEGER, n_review INTEGER, n_no_match INTEGER, run_by VARCHAR
);
CREATE TABLE IF NOT EXISTS audit (
    run_id VARCHAR, bank_code VARCHAR, bank_row_id VARCHAR, raw_name VARCHAR,
    old_status VARCHAR, old_inhouse_id VARCHAR, new_inhouse_id VARCHAR,
    decided_by VARCHAR, decided_at TIMESTAMP
);
"""


def workspace_path() -> Path:
    home = Path(os.environ.get("ENTITY_MAPPER_HOME", Path.home() / "EntityMapper"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "workspace.duckdb"


class Workspace:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else workspace_path()
        self._con = duckdb.connect(str(self.path))
        self._con.execute(SCHEMA)
        self._lock = threading.Lock()

    @property
    def con(self):
        """A fresh cursor per call: DuckDB connections are not safe to share across threads."""
        return self._con.cursor()

    def aliases_for(self, bank_code: str) -> dict:
        rows = self.con.execute(
            "SELECT name_clean, country, inhouse_id FROM aliases WHERE bank_code = ?",
            [bank_code]).fetchall()
        return {(n, c or ""): i for n, c, i in rows}

    def log_run(self, bank_code: str, results: pd.DataFrame) -> str:
        run_id = uuid.uuid4().hex[:12]
        vc = results["status"].value_counts()
        self.con.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, bank_code, now_utc(), len(results),
             int(vc.get("auto", 0)), int(vc.get("review", 0)), int(vc.get("no_match", 0)),
             current_user()])
        return run_id

    def save_decisions(self, bank_code: str, run_id: str, decisions: pd.DataFrame,
                       user: str | None = None) -> int:
        """decisions needs: bank_row_id, bank_name, bank_country, clean_name, status,
        inhouse_id (old), decision_inhouse_id (new; '' or NO_MATCH for no match)."""
        user = user or current_user()
        now = now_utc()
        n = 0
        for d in decisions.itertuples(index=False):
            new = d.decision_inhouse_id or NO_MATCH
            country = normalize_country(d.bank_country)
            self.con.execute(
                "INSERT OR REPLACE INTO aliases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [bank_code, d.clean_name, country, new, d.bank_name, user, now, run_id])
            self.con.execute(
                "INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, bank_code, str(d.bank_row_id), d.bank_name, d.status,
                 d.inhouse_id, new, user, now])
            n += 1
        return n

    # ---- shared folder sync: the merged shared state replaces the local copy ----
    def alias_records(self, user: str | None = None) -> list[dict]:
        """Decisions as plain records (decided_at as ISO text), optionally one user's only."""
        df = self.con.execute("SELECT * FROM aliases" + (" WHERE decided_by = ?" if user else ""),
                              [user] if user else []).df()
        df["decided_at"] = pd.to_datetime(df["decided_at"]).dt.strftime("%Y-%m-%dT%H:%M:%S")
        return df.astype(object).where(pd.notna(df), None).to_dict("records")

    def replace_aliases(self, records: list[dict]) -> int:
        cols = ["bank_code", "name_clean", "country", "inhouse_id", "raw_name", "decided_by", "decided_at", "run_id"]
        df = pd.DataFrame(records, columns=cols)
        df["decided_at"] = pd.to_datetime(df["decided_at"], errors="coerce")
        df = df.drop_duplicates(subset=["bank_code", "name_clean", "country"], keep="last")
        con = self.con
        with self._lock:
            con.execute("BEGIN")
            con.execute("DELETE FROM aliases")
            con.register("merged_aliases", df)
            con.execute("INSERT INTO aliases SELECT * FROM merged_aliases")
            con.execute("COMMIT")
        return len(df)

    def replace_master(self, df: pd.DataFrame) -> int:
        d = df.reindex(columns=["name", "country", "inhouse_id", "bank_code", "source", "imported_at"]).copy()
        d["imported_at"] = pd.to_datetime(d["imported_at"], errors="coerce")
        con = self.con
        with self._lock:
            con.execute("BEGIN")
            con.execute("DELETE FROM master_map")
            con.register("shared_master", d)
            con.execute("INSERT INTO master_map SELECT * FROM shared_master")
            con.execute("COMMIT")
        return len(d)

    def set_alias(self, bank_code: str, name_clean: str, country: str, inhouse_id: str, raw_name: str) -> None:
        """One decision made outside a run (the owner settling a conflict)."""
        self.con.execute("INSERT OR REPLACE INTO aliases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         [bank_code, name_clean, country or "", inhouse_id or NO_MATCH, raw_name,
                          current_user(), now_utc(), "conflict"])

    def master_rows(self) -> pd.DataFrame:
        return self.con.execute("SELECT * FROM master_map").df()

    # ---- master mapping: previous human-verified mappings, any bank ----
    def import_master(self, df: pd.DataFrame, source: str, replace: bool = False) -> int:
        """df columns: name, inhouse_id, optional country, bank_code."""
        d = df.copy()
        for c in ("country", "bank_code"):
            if c not in d:
                d[c] = ""
        d = d[["name", "country", "inhouse_id", "bank_code"]].fillna("").astype(str)
        d = d[(d["name"].str.strip() != "") & (d["inhouse_id"].str.strip() != "")]
        d["inhouse_id"] = d["inhouse_id"].str.strip().str.upper()
        d["bank_code"] = d["bank_code"].str.strip().str.upper()
        d["source"] = source
        d["imported_at"] = now_utc()
        con = self.con
        if replace:
            con.execute("DELETE FROM master_map")
        con.register("incoming", d)
        con.execute("INSERT INTO master_map SELECT name, country, inhouse_id, bank_code, source, imported_at FROM incoming")
        return len(d)

    def master_df(self) -> pd.DataFrame:
        """Imported master mapping plus every reviewer decision (all banks)."""
        return self.con.execute("""
            SELECT name, country, inhouse_id, bank_code FROM master_map
            UNION ALL
            SELECT raw_name AS name, country, inhouse_id, bank_code FROM aliases
            WHERE inhouse_id <> '__NO_MATCH__'
        """).df()

    def master_summary(self) -> dict:
        con = self.con
        n, banks, last = con.execute(
            "SELECT count(*), count(DISTINCT bank_code), max(imported_at) FROM master_map").fetchone()
        n_dec = con.execute("SELECT count(*) FROM aliases WHERE inhouse_id <> '__NO_MATCH__'").fetchone()[0]
        return {"imported_rows": n, "banks": banks, "last_import": str(last) if last else None,
                "reviewer_decisions": n_dec}

    def alias_table(self) -> pd.DataFrame:
        return self.con.execute("SELECT * FROM aliases ORDER BY decided_at DESC").df()

    def runs(self) -> pd.DataFrame:
        return self.con.execute("SELECT * FROM runs ORDER BY started_at DESC").df()
