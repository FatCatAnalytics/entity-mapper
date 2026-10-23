"""FastAPI backend for Entity Mapper.

Wraps the Python matching engine and serves the built React app from web/dist.
Binds to 127.0.0.1 only (see run.py). No data leaves this machine.
"""
from __future__ import annotations

import io
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd
from unidecode import unidecode
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from entity_mapper import (NO_MATCH, Config, Workspace, build_master_index, build_reference,
                           load_gleif_subset, match)
from entity_mapper.gleif import name_country_index, scan_golden_copy
from entity_mapper.leicheck import lei_check as _lei_check, names_agree
from entity_mapper.normalize import clean_name
from entity_mapper.matcher import BANK_COLS, INHOUSE_COLS
from entity_mapper.normalize import lei_is_valid, normalize_country
from entity_mapper.shared import Settings, SharedFolder, current_user, local_reference, now_utc
from entity_mapper.sync import push_own, sync_shared

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "web" / "dist"
if getattr(sys, "frozen", False) and not DIST.exists():  # packaged engine: files sit beside the code
    DIST = Path(getattr(sys, "_MEIPASS", ROOT)) / "web" / "dist"

ws = Workspace()
HOME = ws.path.parent
UPLOADS = HOME / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
LOCAL_REF = HOME / "reference"
OWNER_DIR = HOME / "owner"
settings = Settings(HOME)

app = FastAPI(title="Entity Mapper", docs_url="/api/docs", openapi_url="/api/openapi.json")

STATE: dict = {"ref": None, "ref_info": None, "runs": {}, "sync": None, "built": None}
JOBS: dict = {}
LOCK = threading.Lock()

GUESS = {
    "inhouse_id": ["coalition id", "coalition_id", "inhouse_id", "id", "entity_id", "client_id", "internal_id", "code"],
    "bank_row_id": ["client_leid", "bank_row_id", "row_id", "id", "client_id", "ref", "reference"],
    "name": ["client_lename", "granular name", "name", "legal_name", "entity_name", "client_name", "company", "company_name"],
    "country": ["country", "country_code", "domicile", "jurisdiction", "country_of_incorporation"],
    "city": ["city", "town"],
    "lei": ["lei", "lei_code", "legal_entity_identifier"],
    "ciq_id": ["client_leid", "ciq_id", "capiq_id", "capital_iq_id", "companyid", "company_id", "spciqid"],
    "bank_code": ["bank_code", "bank", "submitter", "source_bank"],
    "status": ["status", "mapped"],
    "coalition_names": ["coalition names", "coalition_name", "coalition names"],
    "mapped_at": ["mapped at", "mapped_at", "timestamp"],
    "source": ["source", "mapped by", "mapped_by"],
}
MASTER_COLS = ["name", "inhouse_id", "country", "bank_code"]
PERIOD_MASTER_COLS = ["name", "inhouse_id", "status", "coalition_names", "mapped_at", "source"]
PERIOD_DELIVERED_COLS = ["name", "ciq_id", "inhouse_id", "status", "coalition_names"]
PERIOD_RECEIVED_COLS = ["name", "ciq_id", "country", "bank_row_id"]


def _read_csv_sep(path: Path, sep: str, header: int | None = 0) -> pd.DataFrame:
    """Read a delimited text file. Prefer the C engine (keeps odd quotes); fall back to Python."""
    common = dict(dtype=str, sep=sep, header=header, encoding_errors="replace",
                  keep_default_na=False, skip_blank_lines=True)
    try:
        return pd.read_csv(path, engine="c", **common)
    except Exception:
        return pd.read_csv(path, engine="python", on_bad_lines="warn", **common)


def _sniff_delimiter(path: Path) -> str | None:
    """Best-effort delimiter from the header line (handles tab-separated .csv exports)."""
    import csv
    try:
        sample = path.read_bytes()[:16384].decode("utf-8", errors="replace")
    except Exception:
        return None
    if not sample.strip():
        return None
    header = sample.splitlines()[0] if sample.splitlines() else sample
    counts = {d: header.count(d) for d in ("\t", ",", ";", "|")}
    best, n = max(counts.items(), key=lambda kv: kv[1])
    if n > 0:
        return best
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t|;").delimiter
    except Exception:
        return None


def _read(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str)
    if path.suffix.lower() == ".tsv":  # pasted from a spreadsheet: tabs, never guessed
        header = 0 if ".hdr" in path.suffixes else None
        df = _read_csv_sep(path, "\t", header=header)
        if header is None:
            df.columns = [f"Column {i + 1}" for i in range(df.shape[1])]
        return df

    # .csv / .txt: try sniffed delimiter, then common ones. Tab-separated bank exports
    # often keep a .csv extension and break pandas' default comma reader.
    seps: list[str] = []
    sniffed = _sniff_delimiter(path)
    if sniffed:
        seps.append(sniffed)
    for s in ("\t", ",", ";", "|"):
        if s not in seps:
            seps.append(s)

    last_err: Exception | None = None
    best: pd.DataFrame | None = None
    for sep in seps:
        try:
            df = _read_csv_sep(path, sep)
        except Exception as e:
            last_err = e
            continue
        if df.shape[1] <= 1 and best is not None:
            continue  # single-column parse is usually a wrong delimiter
        if best is None or df.shape[1] > best.shape[1]:
            best = df
        if df.shape[1] > 1:
            return df
    if best is not None:
        return best
    raise HTTPException(
        400,
        f"Could not read {path.suffix} file"
        + (f": {last_err}" if last_err else "")
        + ". Check quoting and separators, or save it as .xlsx and try again.",
    )


def _file(file_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", file_id or ""):
        raise HTTPException(400, "Bad file reference.")
    hits = list(UPLOADS.glob(f"{file_id}.*"))
    if not hits:
        raise HTTPException(404, "Upload not found; please upload the file again.")
    return hits[0]


CONTAINS = {  # fallback: header contains one of these words ("Coalition Names", "Country HQ")
    "inhouse_id": ["coalition id", " id", "_id", "id "], "bank_row_id": ["leid", " id", "_id", "ref"],
    "name": ["lename", "granular", "name"], "country": ["country", "domicile"], "city": ["city"],
    "lei": ["lei"], "ciq_id": ["leid", "ciq", "capiq", "capital iq"], "bank_code": ["bank"],
    "status": ["status"], "coalition_names": ["coalition name"], "mapped_at": ["mapped at", "timestamp"],
    "source": ["source"],
}


def _guess(columns: list[str], fields: list[str]) -> dict:
    lower = {c.lower().strip(): c for c in columns}
    out, used = {}, set()
    for f in fields:
        hit = next((lower[g] for g in GUESS.get(f, []) if g in lower and lower[g] not in used), None)
        if not hit:
            hit = next((c for c in columns if c not in used and any(
                k in f" {c.lower().strip()} " for k in CONTAINS.get(f, []))), None)
        out[f] = hit
        if hit:
            used.add(hit)
    return out


def _apply(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    return pd.DataFrame({f: df[c] for f, c in mapping.items() if c})


def _clean_records(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(pd.notna(df), None).to_dict("records")


# ---------- uploads ----------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...), kind: str = "bank", header: int = 1):
    suffix = Path(file.filename or "").suffix.lower() or ".csv"
    if suffix not in (".csv", ".txt", ".tsv", ".xlsx", ".xls"):
        raise HTTPException(400, "Upload a CSV or Excel file.")
    file_id = uuid.uuid4().hex[:12]
    path = UPLOADS / f"{file_id}{'.hdr' if suffix == '.tsv' and header else ''}{suffix}"
    data = await file.read()
    if not data.strip():
        raise HTTPException(400, "That is empty: nothing to read.")
    path.write_bytes(data)
    STATE.setdefault("filenames", {})[file_id] = file.filename
    if kind == "universe":
        if suffix not in (".xlsx", ".xls"):
            raise HTTPException(400, "The universe master file is the .xlsx made by build_universe_master.py.")
        um = pd.read_excel(path, sheet_name="universe master", dtype=str)
        return {"file_id": file_id, "filename": file.filename, "rows": len(um), "columns": list(um.columns),
                "suggested": {}, "fields": [], "preview": _clean_records(um.head(5).fillna("")),
                "roles": um["role"].value_counts().to_dict()}
    df = _read(path)
    fields = {
        "inhouse": INHOUSE_COLS,
        "master": MASTER_COLS,
        "period_master": PERIOD_MASTER_COLS,
        "period_delivered": PERIOD_DELIVERED_COLS,
        "period_received": PERIOD_RECEIVED_COLS,
        "period_capiq": ["ciq_id", "inhouse_id"],
    }.get(kind, BANK_COLS)
    if kind == "master":  # the bank's spelling usually sits in a column like bank_name
        GUESS["name"] = ["bank_name", "bank_entity_name", "client_name", "name", "entity_name"]
    elif kind == "period_master":
        GUESS["name"] = ["granular name", "granular_name", "name", "entity_name", "client_lename"]
        GUESS["inhouse_id"] = ["coalition id", "coalition_id", "inhouse_id"]
    elif kind in ("period_delivered", "period_received", "bank"):
        GUESS["name"] = ["client_lename", "lename", "name", "legal_name", "entity_name", "client_name", "company"]
        GUESS["ciq_id"] = ["client_leid", "leid", "ciq_id", "capiq_id", "companyid"]
        GUESS["inhouse_id"] = ["coalition id", "coalition_id", "inhouse_id"]
    else:
        GUESS["name"] = ["name", "legal_name", "entity_name", "client_name", "company", "company_name"]
    return {"file_id": file_id, "filename": file.filename, "rows": len(df),
            "columns": list(df.columns), "suggested": _guess(list(df.columns), fields),
            "fields": fields, "preview": _clean_records(df.head(5).fillna(""))}


# ---------- reference ----------
class RefRequest(BaseModel):
    inhouse_file_id: Optional[str] = None
    universe_file_id: Optional[str] = None
    mapping: dict
    gleif_l1_path: Optional[str] = None
    gleif_rr_path: Optional[str] = None
    ciq_file_id: Optional[str] = None


@app.post("/api/reference")
def build_ref(req: RefRequest):
    if req.universe_file_id:
        return _build_from_universe(req)
    if not req.inhouse_file_id:
        raise HTTPException(400, "Load the universe master file, or an in-house list.")
    if not req.mapping.get("inhouse_id") or not req.mapping.get("name"):
        raise HTTPException(400, "Map at least the in-house ID and name columns.")
    t0 = time.time()
    inh = _apply(_read(_file(req.inhouse_file_id)), req.mapping)
    gleif, name_index, lei_info = None, None, {}
    if req.gleif_l1_path:
        p = Path(req.gleif_l1_path).expanduser()
        if not p.exists():
            raise HTTPException(400, f"GLEIF file not found: {p}")
        rr = Path(req.gleif_rr_path).expanduser() if req.gleif_rr_path else None
        if rr and not rr.exists():
            raise HTTPException(400, f"GLEIF relationship file not found: {rr}")
        # One pass over the Golden Copy (CSV or .csv.zip); cached, so rebuilds are instant.
        scan = scan_golden_copy(p, set(inh.get("lei", pd.Series(dtype=str)).dropna()),
                                {clean_name(n) for n in inh["name"].dropna()}, rr_path=rr,
                                cache_dir=ws.path.parent / "gleif_cache")
        gleif, name_index, lei_info = scan["subset"], scan["name_index"], scan["lei_info"]
    ciq = _read(_file(req.ciq_file_id)) if req.ciq_file_id else None
    master = ws.master_df()
    ref = build_reference(inh, gleif, ciq, name_index=name_index, master=master)
    with LOCK:
        STATE["ref"] = ref
        names_by_lei = ({r.lei: r.names for r in gleif.itertuples() if not r.parent_lei}
                        if gleif is not None else {})
        STATE["lei_check"] = _lei_check(inh, lei_info, names_by_lei) if req.gleif_l1_path else None
        STATE["ref_info"] = {**ref.stats, "built_in_s": round(time.time() - t0, 1),
                             "master_rows": len(master), "gleif_name_index": bool(name_index),
                             "gleif_subsidiaries": int((gleif["parent_lei"] != "").sum()) if gleif is not None else 0,
                             "lei_issues": (STATE["lei_check"]["severity"].value_counts().to_dict()
                                            if STATE["lei_check"] is not None else None)}
    return STATE["ref_info"]


@app.get("/api/reference/lei-check")
def lei_check():
    df = STATE.get("lei_check")
    if df is None:
        raise HTTPException(404, "Build the reference with the GLEIF Golden Copy first.")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df[df["severity"] == "action"].to_excel(xw, sheet_name="fix these", index=False)
        df[df["severity"] == "check"].to_excel(xw, sheet_name="worth a look", index=False)
        df.to_excel(xw, sheet_name="all LEIs", index=False)
    buf.seek(0)
    return StreamingResponse(buf, headers={"Content-Disposition": 'attachment; filename="lei_check.xlsx"'},
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _build_from_universe(req: RefRequest):
    path = _file(req.universe_file_id)
    return _load_universe(path, STATE.get("filenames", {}).get(req.universe_file_id, path.name))


def _load_universe(path: Path, label: str, published: Optional[dict] = None):
    """Reference from the prebuilt universe master: no GLEIF files, no network."""
    from entity_mapper.universe import reference_inputs
    t0 = time.time()
    xl = pd.ExcelFile(path)
    um = xl.parse("universe master", dtype=str).fillna("")
    inh, subset, extra_ciq = reference_inputs(um)
    name_index = None
    if "name countries" in xl.sheet_names:
        nc = xl.parse("name countries", dtype=str).fillna("")
        name_index = {n: set(c.split(",")) - {""} for n, c in zip(nc["name"], nc["countries"])}
    master = ws.master_df()
    ref = build_reference(inh, subset, None, name_index=name_index, master=master, extra_ciq=extra_ciq)
    check = xl.parse("LEI check", dtype=str).fillna("") if "LEI check" in xl.sheet_names else None
    roles = um["role"].value_counts().to_dict()
    with LOCK:
        STATE["ref"] = ref
        STATE["lei_check"] = check
        STATE["ref_info"] = {**ref.stats, "built_in_s": round(time.time() - t0, 1), "source": "universe master",
                             "universe_file": label, "roles": roles, "published": published,
                             "master_rows": len(master), "gleif_name_index": bool(name_index),
                             "gleif_subsidiaries": int(roles.get("subsidiary", 0)),
                             "lei_issues": (check["severity"].value_counts().to_dict() if check is not None else None)}
    return STATE["ref_info"]


@app.get("/api/reference")
def get_ref():
    return STATE["ref_info"]


class MasterImport(BaseModel):
    file_id: str
    mapping: dict
    replace: bool = False


@app.post("/api/master/import")
def import_master(req: MasterImport):
    if not req.mapping.get("name") or not req.mapping.get("inhouse_id"):
        raise HTTPException(400, "Map the bank's entity name and the in-house ID columns.")
    path = _file(req.file_id)
    df = _apply(_read(path), req.mapping)
    source = STATE.get("filenames", {}).get(req.file_id, path.name)
    shared_note = None
    sh = _shared()
    if sh and sh.reachable() and sh.master_path.exists():
        ws.replace_master(sh.read_master())  # start from what everyone has, then add this import
    n = ws.import_master(df, source=source, replace=req.replace)
    if sh and sh.reachable():
        try:
            local = ws.master_rows()
            local["imported_at"] = local["imported_at"].astype(str)
            sh.write_master(local, replace=True)  # local = shared + this import (or just this, on replace)
            shared_note = "Saved to the shared folder: everyone gets it on their next start."
        except OSError as e:
            shared_note = f"Imported here, but the shared folder could not be written ({e})."
    out = {"imported": n, **ws.master_summary(), "shared_note": shared_note}
    if STATE["ref"] is not None:
        _, st = build_master_index(ws.master_df(), STATE["ref"].inhouse.index)
        out["unknown_inhouse_ids"] = st["unknown_inhouse_id"]
        out["note"] = "Rebuild the reference index so these spellings are also used for fuzzy matching."
    return out


@app.get("/api/master")
def master_summary():
    return ws.master_summary()


@app.get("/api/inhouse/search")
def search_inhouse(q: str, limit: int = 20):
    ref = STATE["ref"]
    if ref is None:
        raise HTTPException(400, "Build the reference first.")
    inh = ref.inhouse
    ql = q.strip().lower()
    hit = inh[inh["name"].str.lower().str.contains(ql, regex=False) |
              (inh["inhouse_id"].str.lower() == ql)]
    return _clean_records(hit[["inhouse_id", "name", "country", "lei"]].head(limit))


# ---------- matching ----------
class MatchRequest(BaseModel):
    bank_file_id: str
    mapping: dict
    bank_code: str
    auto_score: float = 0.92
    auto_margin: float = 0.06
    review_score: float = 0.80
    auto_subsidiaries: bool = True
    containment_review: bool = True
    default_country: str = ""
    flag_possible_subsidiaries: bool = True


def _profile(bank: pd.DataFrame) -> dict:
    """Share of rows with each field filled, and what that means for matching."""
    fill = {c: (round(float(bank[c].fillna("").astype(str).str.strip().ne("").mean()), 3)
                if c in bank.columns else 0.0)
            for c in ["name", "country", "lei", "ciq_id"]}
    notes = []
    if fill.get("country", 0) < 0.5:
        notes.append("Few or no countries: country checks rely on legal forms and the GLEIF name check.")
    if fill.get("lei", 0) == 0 and fill.get("ciq_id", 0) == 0:
        notes.append("No identifiers: matching is by name only.")
    return {"fill": fill, "notes": notes}


def _run_job(run_id: str, req: MatchRequest):
    run = STATE["runs"][run_id]
    try:
        bank = _apply(_read(_file(req.bank_file_id)), req.mapping).reset_index(drop=True)
        if "bank_row_id" not in bank or bank["bank_row_id"].isna().any() or bank["bank_row_id"].duplicated().any():
            bank["bank_row_id"] = (bank.index + 1).astype(str)  # IDs must be unique for review
        cfg = Config(auto_score=req.auto_score, auto_margin=req.auto_margin,
                     review_score=req.review_score, auto_subsidiaries=req.auto_subsidiaries,
                     containment_review=req.containment_review,
                     default_country=req.default_country.strip(),
                     flag_possible_subsidiaries=req.flag_possible_subsidiaries)
        run["profile"] = _profile(bank)
        if cfg.default_country:
            run["profile"]["notes"].append(f"Rows without a country treated as {cfg.default_country.upper()}.")
        master_index, mstats = build_master_index(ws.master_df(), STATE["ref"].inhouse.index)
        run["master"] = mstats
        run["stage"] = "matching"

        def prog(p):
            run["progress"] = round(0.1 + 0.6 * p, 3)
        res = match(bank, STATE["ref"], cfg, ws.aliases_for(req.bank_code), progress=prog,
                    master_index=master_index)
        res["decided"] = ""
        run["results"] = res
        run["summary"] = res["status"].value_counts().to_dict()
        run["by_tier"] = res.groupby(["tier", "status"]).size().unstack(fill_value=0).to_dict("index")
        run["db_run_id"] = ws.log_run(req.bank_code, res)
        run["seconds"] = round(time.time() - run["started"], 1)
        run["progress"] = 1.0
        run["stage"] = "done"
    except Exception as e:  # surfaced to the UI
        run["stage"] = "error"
        run["error"] = str(e)


@app.post("/api/match")
def start_match(req: MatchRequest):
    if STATE["ref"] is None:
        raise HTTPException(400, "Build the reference first.")
    if not req.bank_code.strip():
        raise HTTPException(400, "Enter a bank code.")
    if not req.mapping.get("name"):
        raise HTTPException(400, "Map the bank name column.")
    req.bank_code = req.bank_code.strip().upper()
    run_id = uuid.uuid4().hex[:10]
    STATE["runs"][run_id] = {"run_id": run_id, "bank_code": req.bank_code, "stage": "loading",
                             "progress": 0.05, "started": time.time()}
    threading.Thread(target=_run_job, args=(run_id, req), daemon=True).start()
    return {"run_id": run_id}


def _run(run_id: str) -> dict:
    run = STATE["runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found (runs are kept in memory until the app restarts).")
    return run


@app.get("/api/runs/{run_id}")
def run_status(run_id: str):
    run = _run(run_id)
    # DataFrames (results, master) stay in memory; only JSON-safe fields go to the UI.
    return {k: v for k, v in run.items() if k not in ("results", "master")}


@app.get("/api/runs/{run_id}/results")
def run_results(run_id: str, status: str = "review", page: int = 1, size: int = 50, q: str = ""):
    run = _run(run_id)
    res = run.get("results")
    if res is None:
        raise HTTPException(409, "Run not finished.")
    view = res[res["status"] == status] if status != "all" else res
    if q:
        ql = q.lower()
        view = view[view["bank_name"].str.lower().str.contains(ql, regex=False) |
                    view["inhouse_name"].fillna("").str.lower().str.contains(ql, regex=False)]
    if status == "review":
        view = view.sort_values("score", ascending=False)
    total = len(view)
    rows = view.iloc[(page - 1) * size: page * size]
    return {"total": total, "page": page, "size": size, "rows": _clean_records(rows)}


class Decision(BaseModel):
    bank_row_id: str
    inhouse_id: Optional[str] = None   # None / "" = no match


@app.post("/api/runs/{run_id}/decisions")
def save_decisions(run_id: str, decisions: list[Decision]):
    run = _run(run_id)
    res = run["results"]
    ref = STATE["ref"]
    idx = res.set_index("bank_row_id")
    rows = []
    for d in decisions:
        if d.bank_row_id not in idx.index:
            continue
        new = (d.inhouse_id or "").strip().upper()
        if new and new not in ref.inhouse.index:
            raise HTTPException(400, f"Unknown in-house ID: {new}")
        r = idx.loc[d.bank_row_id]
        rows.append({"bank_row_id": d.bank_row_id, "bank_name": r["bank_name"],
                     "bank_country": r["bank_country"], "clean_name": r["clean_name"],
                     "status": r["status"], "inhouse_id": r["inhouse_id"],
                     "decision_inhouse_id": new or NO_MATCH})
    if not rows:
        return {"saved": 0}
    dec = pd.DataFrame(rows)
    n = ws.save_decisions(run["bank_code"], run.get("db_run_id", run_id), dec)
    shared_saved = _push_decisions()
    upd = dec.set_index("bank_row_id")["decision_inhouse_id"].replace(NO_MATCH, "")
    mask = res["bank_row_id"].isin(upd.index)
    res.loc[mask, "inhouse_id"] = res.loc[mask, "bank_row_id"].map(upd)
    res.loc[mask, "inhouse_name"] = res.loc[mask, "inhouse_id"].map(ref.inhouse["name"]).fillna("")
    res.loc[mask, "status"] = res.loc[mask, "inhouse_id"].map(lambda x: "manual" if x else "no_match")
    res.loc[mask, "tier"] = "reviewer"
    res.loc[mask, "decided"] = "yes"
    run["summary"] = res["status"].value_counts().to_dict()
    return {"saved": n, "summary": run["summary"], "shared": shared_saved}


@app.get("/api/runs/{run_id}/export")
def export(run_id: str):
    run = _run(run_id)
    res = run["results"]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for s in ["auto", "manual", "review", "no_match"]:
            part = res[res["status"] == s]
            if len(part):
                part.drop(columns=["decided"], errors="ignore").to_excel(xw, sheet_name=s, index=False)
        if run.get("kind") == "period":
            delivery = res[[c for c in [
                "client_corigrpcomid", "client_corigrpname", "client_leid", "client_lename",
                "Status", "Coalition ID", "Coalition Names", "tier", "evidence", "score",
                "parent_hint", "inhouse_id_2", "inhouse_name_2", "inhouse_id_3", "inhouse_name_3",
            ] if c in res.columns]]
            delivery.to_excel(xw, sheet_name="delivery", index=False)
            if run.get("master") is not None:
                run["master"].to_excel(xw, sheet_name="master_updated", index=False)
    buf.seek(0)
    name = f"{run['bank_code']}_mapping_{run_id}.xlsx"
    return StreamingResponse(buf, headers={"Content-Disposition": f'attachment; filename="{name}"'},
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------- period mapping (master + prior delivery + received) ----------
class PeriodRequest(BaseModel):
    master_file_id: str
    delivered_file_id: str
    received_file_id: str
    master_mapping: dict = {}
    delivered_mapping: dict = {}
    received_mapping: dict = {}
    bank_code: str = "PERIOD"
    auto_score: float = 0.92
    auto_margin: float = 0.06
    review_score: float = 0.80
    auto_subsidiaries: bool = True
    containment_review: bool = True
    default_country: str = ""
    flag_possible_subsidiaries: bool = True
    # structural / master tiers
    use_parent_group: bool = True
    parent_majority_share: float = 0.8
    parent_min_siblings: int = 2
    use_parent_name: bool = True
    use_master_contain: bool = True
    use_master_fuzzy: bool = True
    master_fuzzy_auto: float = 95.0
    master_fuzzy_review: float = 90.0
    master_fuzzy_margin: float = 5.0
    use_capiq: bool = True
    conflict_audit: bool = True
    capiq_file_id: Optional[str] = None       # optional extra CapIQ id → Coalition ID list
    capiq_mapping: dict = {}                   # {"ciq_id": col, "inhouse_id": col}


def _ensure_period_ref():
    """Use the in-memory universe reference, or load the local published one."""
    if STATE["ref"] is not None:
        return STATE["ref_info"]
    ref_path, manifest = local_reference(LOCAL_REF)
    if not ref_path:
        return None
    return _load_universe(ref_path, f"published {manifest.get('version', '')}".strip(), manifest)


def _period_job(run_id: str, req: PeriodRequest):
    from entity_mapper.period import (PeriodOptions, norm_capiq, period_match, prepare_delivered,
                                      prepare_master, prepare_received)

    run = STATE["runs"][run_id]
    try:
        run["stage"] = "loading"
        master = prepare_master(_read(_file(req.master_file_id)), req.master_mapping)
        delivered = prepare_delivered(_read(_file(req.delivered_file_id)), req.delivered_mapping)
        received = prepare_received(_read(_file(req.received_file_id)), req.received_mapping)
        if received["name"].fillna("").astype(str).str.strip().eq("").all():
            raise ValueError("Map the received entity-name column (client_lename).")

        run["progress"] = 0.1
        run["stage"] = "matching"
        ref_info = _ensure_period_ref()
        cfg = Config(auto_score=req.auto_score, auto_margin=req.auto_margin,
                     review_score=req.review_score, auto_subsidiaries=req.auto_subsidiaries,
                     containment_review=req.containment_review,
                     default_country=req.default_country.strip(),
                     flag_possible_subsidiaries=req.flag_possible_subsidiaries)
        opts = PeriodOptions(
            use_parent_group=req.use_parent_group, parent_majority_share=req.parent_majority_share,
            parent_min_siblings=req.parent_min_siblings, use_parent_name=req.use_parent_name,
            use_master_contain=req.use_master_contain, use_master_fuzzy=req.use_master_fuzzy,
            master_fuzzy_auto=req.master_fuzzy_auto, master_fuzzy_review=req.master_fuzzy_review,
            master_fuzzy_margin=req.master_fuzzy_margin, use_capiq=req.use_capiq,
            conflict_audit=req.conflict_audit,
        )

        # Universe sheet (Coalition names + CapIQ ids) when the published file is on disk
        universe = None
        ref_path, _ = local_reference(LOCAL_REF)
        if ref_path:
            try:
                universe = pd.read_excel(ref_path, sheet_name="universe master", dtype=str).fillna("")
            except Exception:
                universe = None
        # Optional CapIQ bridge file: ciq_id → Coalition ID, appended to the universe ids
        if req.capiq_file_id:
            raw_cq = _read(_file(req.capiq_file_id))
            cq = _apply(raw_cq, req.capiq_mapping or _guess(list(raw_cq.columns), ["ciq_id", "inhouse_id"]))
            if "ciq_id" in cq and "inhouse_id" in cq:
                extra = pd.DataFrame({
                    "coalition_id": cq["inhouse_id"].fillna("").astype(str).str.strip(),
                    "coalition_name": "",
                    "ciq_id": cq["ciq_id"].fillna("").astype(str).map(norm_capiq),
                })
                extra = extra[(extra["coalition_id"] != "") & (extra["ciq_id"] != "")]
                universe = pd.concat([universe, extra], ignore_index=True) if universe is not None else extra

        def prog(p):
            run["progress"] = round(0.1 + 0.8 * p, 3)

        results, updated_master, stats = period_match(
            received, delivered, master, ref=STATE["ref"], cfg=cfg, progress=prog, opts=opts, universe=universe)
        results["decided"] = ""
        run["results"] = results
        run["master"] = updated_master
        run["summary"] = {
            "auto": stats["auto_mapped"],
            "review": stats["review"],
            "no_match": stats["no_match"],
            **{k: v for k, v in stats["by_status"].items() if k not in ("auto", "review", "no_match")},
        }
        run["by_tier"] = {t: {"auto": int(((results["tier"] == t) & (results["status"] == "auto")).sum()),
                              "review": int(((results["tier"] == t) & (results["status"] == "review")).sum()),
                              "no_match": int(((results["tier"] == t) & (results["status"] == "no_match")).sum())}
                          for t in sorted(results["tier"].fillna("").unique()) if t}
        run["period"] = {
            **stats,
            "reference_loaded": ref_info is not None,
            "reference": ref_info,
            "delivered_rows": len(delivered),
            "master_rows_in": len(master),
        }
        run["profile"] = _profile(received.rename(columns={"ciq_id": "ciq_id"}))
        run["db_run_id"] = ws.log_run(req.bank_code, results)
        # Also seed workspace master_map from prior delivery + new autos (name → coalition id)
        seed = results[results["status"] == "auto"][["client_lename", "Coalition ID"]].rename(
            columns={"client_lename": "name", "Coalition ID": "inhouse_id"})
        seed = seed[seed["name"].astype(str).str.strip().ne("") & seed["inhouse_id"].astype(str).str.strip().ne("")]
        if len(seed):
            ws.import_master(seed.assign(country="", bank_code=req.bank_code),
                             source=f"period:{run_id}", replace=False)
        run["seconds"] = round(time.time() - run["started"], 1)
        run["progress"] = 1.0
        run["stage"] = "done"
    except Exception as e:
        run["stage"] = "error"
        run["error"] = str(e)


@app.post("/api/period/match")
def start_period(req: PeriodRequest):
    if not req.master_file_id or not req.delivered_file_id or not req.received_file_id:
        raise HTTPException(400, "Upload the master, prior delivery, and received files.")
    req.bank_code = (req.bank_code or "PERIOD").strip().upper()
    run_id = uuid.uuid4().hex[:10]
    STATE["runs"][run_id] = {
        "run_id": run_id, "bank_code": req.bank_code, "kind": "period",
        "stage": "loading", "progress": 0.05, "started": time.time(),
    }
    threading.Thread(target=_period_job, args=(run_id, req), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}/master")
def export_master(run_id: str):
    """Download the master list after period write-backs (CSV)."""
    run = _run(run_id)
    master = run.get("master")
    if master is None:
        raise HTTPException(404, "No updated master for this run (only period runs produce one).")
    buf = io.StringIO()
    master.to_csv(buf, index=False)
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    name = f"master_updated_{run_id}.csv"
    return StreamingResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"'},
                             media_type="text/csv")


# ---------- memory ----------
@app.get("/api/aliases")
def aliases(limit: int = 500):
    return _clean_records(ws.alias_table().head(limit).astype(str))


@app.get("/api/history")
def history():
    return _clean_records(ws.runs().astype(str))


@app.get("/api/health")
def health():
    return {"ok": True, "workspace": str(ws.path)}


# ---------- validation of an incoming file ----------
class ValidateRequest(BaseModel):
    file_id: str
    mapping: dict


_SCI = re.compile(r"^\d(\.\d+)?[eE]\+\d+$")


@app.post("/api/validate")
def validate(req: ValidateRequest):
    """Problems worth knowing before matching: damaged IDs, blanks, repeats, unreadable characters."""
    raw = _read(_file(req.file_id))
    df = _apply(raw, req.mapping).fillna("").astype(str).apply(lambda c: c.str.strip())
    issues = []

    def add(level, text, mask, col):
        n = int(mask.sum())
        if n:
            issues.append({"level": level, "count": n, "text": text,
                           "examples": [f"row {i + 2}: {v}" if v else f"row {i + 2}"
                                        for i, v in df.loc[mask, col].head(3).items()]})

    if "name" not in df:
        return {"rows": len(raw), "ok": False,
                "issues": [{"level": "error", "count": len(raw), "text": "No name column is mapped.", "examples": []}]}
    name = df["name"]
    add("error", "without a name: cannot be matched", name == "", "name")
    add("warn", "with unreadable characters in the name (the file's encoding was damaged; re-export as UTF-8 or .xlsx)",
        name.str.contains("�", regex=False), "name")
    add("warn", "where the name is only numbers (is the right column mapped?)", name.str.fullmatch(r"[\d\s.,-]+"), "name")
    add("warn", "with a name over 200 characters (several entities in one cell?)", name.str.len() > 200, "name")
    key = name.str.lower() + "|" + (df["country"].str.lower() if "country" in df else "")
    add("info", "repeating an earlier name" + (" and country" if "country" in df else "") + " (each is matched; nothing is dropped)",
        key.duplicated() & (name != ""), "name")
    for col, label in (("lei", "LEI"), ("ciq_id", "Capital IQ ID"), ("bank_row_id", "row ID")):
        if col in df:
            add("error", f"with a {label} turned into scientific notation by a spreadsheet (e.g. 5.49E+19): "
                         "re-export with that column formatted as text", df[col].str.fullmatch(_SCI), col)
    if "lei" in df:
        lei = df["lei"]
        sci = lei.str.fullmatch(_SCI)
        add("warn", "with an LEI that fails the checksum: the LEI is ignored and the row is matched by name",
            (lei != "") & ~sci & ~lei.map(lei_is_valid), "lei")
    if "country" in df:
        c = df["country"]
        add("warn", "with a country that is not recognised: treated as blank",
            (c != "") & (c.map(normalize_country) == ""), "country")
    if "bank_row_id" in df:
        rid = df["bank_row_id"]
        if (rid == "").any() or rid.duplicated().any():
            issues.append({"level": "info", "count": int((rid == "").sum() + rid.duplicated().sum()),
                           "text": "with a blank or repeated row ID: rows will be numbered 1, 2, 3… instead", "examples": []})
    return {"rows": len(raw), "ok": not any(i["level"] == "error" for i in issues), "issues": issues}


# ---------- background jobs (startup sync, owner build) ----------
def _start_job(fn) -> dict:
    job_id = uuid.uuid4().hex[:10]
    job = {"job_id": job_id, "stage": "Starting", "progress": None, "log": [], "done": False,
           "error": None, "result": None}
    JOBS[job_id] = job

    def run():
        try:
            job["result"] = fn(job)
        except Exception as e:  # surfaced to the UI
            job["error"] = str(e) or e.__class__.__name__
        finally:
            job["done"] = True
    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return {**job, "log": job["log"][-12:]}


# ---------- shared folder, startup checks ----------
def _shared() -> Optional[SharedFolder]:
    path = settings.shared_folder
    return SharedFolder(path) if path else None


def _push_decisions() -> Optional[bool]:
    """True: in the shared log. False: saved here only, sent on the next sync. None: no shared folder."""
    sh = _shared()
    if not sh:
        return None
    try:
        if not sh.reachable():
            return False
        push_own(ws, sh)
        return True
    except OSError:
        return False


class SettingsRequest(BaseModel):
    shared_folder: str = ""


@app.post("/api/settings")
def save_settings(req: SettingsRequest):
    path = req.shared_folder.strip().strip('"')
    if path and not SharedFolder(path).reachable():
        raise HTTPException(400, f"Folder not found: {path}. Check the path and that the drive is connected.")
    settings.save(shared_folder=path)
    return startup()


@app.get("/api/startup")
def startup():
    """Everything the start screen checks, without changing anything."""
    import os
    sh = _shared()
    reachable = bool(sh and sh.reachable())
    published = sh.reference_manifest() if reachable else None
    _, local = local_reference(LOCAL_REF)
    owner = sh.owner if reachable else ""
    return {"user": current_user(), "workspace": str(HOME),
            "shared": {"configured": bool(sh), "path": str(sh.path) if sh else "", "reachable": reachable,
                       "owner": owner, "is_owner": bool(owner) and owner == current_user(),
                       "from_env": bool(os.environ.get("ENTITY_MAPPER_SHARED"))},
            "reference": {"published": published, "local": local, "loaded": STATE["ref_info"],
                          "update_available": bool(published and (not local or local.get("sha256") != published.get("sha256")))},
            "master": ws.master_summary(), "sync": STATE["sync"]}


def _do_sync(job: dict) -> dict:
    sh = _shared()
    out: dict = {"shared": False, "offline": False, "copied": False, "conflicts": []}
    ref_path, manifest = None, None
    if sh and sh.reachable():
        job["stage"] = "Merging everyone's decisions and the master mapping"
        out.update(sync_shared(ws, sh))
        out["shared"] = True
        job["stage"] = "Checking the published reference"
        ref_path, manifest, out["copied"] = sh.sync_reference(LOCAL_REF)
    else:
        out["offline"] = bool(sh)
        ref_path, manifest = local_reference(LOCAL_REF)
    if ref_path:
        master_n = len(ws.master_df())
        loaded = STATE["ref_info"] or {}
        same = ((loaded.get("published") or {}).get("sha256") == manifest.get("sha256")
                and loaded.get("master_rows") == master_n)
        if not same:
            job["stage"] = "Loading the reference"
            _load_universe(ref_path, f"published {manifest.get('version', '')}".strip(), manifest)
    out["reference_loaded"] = STATE["ref_info"] is not None
    out["at"] = now_utc().isoformat()
    STATE["sync"] = out
    return out


@app.post("/api/startup/sync")
def startup_sync():
    return _start_job(_do_sync)


class Resolve(BaseModel):
    bank_code: str
    name_clean: str
    country: str = ""
    inhouse_id: Optional[str] = None
    raw_name: str = ""


@app.post("/api/shared/resolve")
def resolve_conflict(req: Resolve):
    """The owner settles a disagreement; the owner's decision wins for everyone from their next start."""
    sh = _shared()
    if not sh or not sh.reachable():
        raise HTTPException(400, "The shared folder is not available.")
    if sh.owner != current_user():
        raise HTTPException(403, f"Only the owner ({sh.owner or 'not set yet'}) settles conflicts.")
    ws.set_alias(req.bank_code, req.name_clean, req.country, (req.inhouse_id or "").strip().upper(),
                 req.raw_name or req.name_clean)
    STATE["sync"] = {**(STATE["sync"] or {}), **sync_shared(ws, sh), "shared": True}
    return {"conflicts": STATE["sync"]["conflicts"]}


# ---------- owner: build and publish the reference ----------
def _require_owner(sh: Optional[SharedFolder]) -> SharedFolder:
    if not sh or not sh.reachable():
        raise HTTPException(400, "Set the shared folder first (Setup).")
    if sh.owner and sh.owner != current_user():
        raise HTTPException(403, f"Only the owner ({sh.owner}) publishes the reference.")
    return sh


@app.get("/api/owner/gleif")
def gleif_latest():
    from entity_mapper.gleif_fetch import latest_publish
    try:
        pub = latest_publish()
    except Exception as e:
        raise HTTPException(502, f"Could not reach gleif.org ({e}). Download the two files yourself and point to them below.")
    folder = HOME / "gleif"
    for kind in ("lei2", "rr"):
        f = folder / pub[kind]["name"]
        pub[kind]["downloaded"] = f.exists() and f.stat().st_size == pub[kind]["size"]
    return pub


class BuildRequest(BaseModel):
    inhouse_file_id: str
    mapping: dict
    sp_ids: bool = False
    lei2_path: Optional[str] = None
    rr_path: Optional[str] = None


@app.post("/api/owner/build")
def owner_build(req: BuildRequest):
    if not req.mapping.get("inhouse_id") or not req.mapping.get("name"):
        raise HTTPException(400, "Map at least the in-house ID and name columns.")
    manual = [Path(p.strip().strip('"')).expanduser() for p in (req.lei2_path or "", req.rr_path or "") if p.strip()]
    if len(manual) == 1:
        raise HTTPException(400, "Give both GLEIF files (Level 1 and Level 2), or neither to download them.")
    for p in manual:
        if not p.exists():
            raise HTTPException(400, f"GLEIF file not found: {p}")
    inh_path = _file(req.inhouse_file_id)

    def run(job: dict) -> dict:
        from entity_mapper.gleif_fetch import fetch_golden_copy
        from entity_mapper.universe_build import build_universe_file
        inh = _apply(_read(inh_path), req.mapping)
        if manual:
            lei2, rr = manual
        else:
            job["stage"] = "Downloading the GLEIF Golden Copy"

            def prog(kind, done, total):
                share = {"rr": 0.04, "lei2": 0.96}[kind]
                base = 0.0 if kind == "rr" else 0.04
                job["progress"] = round(0.5 * (base + share * (done / total if total else 0)), 3)
            got = fetch_golden_copy(HOME / "gleif", prog)
            lei2, rr = got["lei2"], got["rr"]
        job["stage"], job["progress"] = "Scanning GLEIF and building the universe master", None
        OWNER_DIR.mkdir(parents=True, exist_ok=True)
        out = OWNER_DIR / f"universe_master_{now_utc():%Y%m%d}.xlsx"

        def log(msg):
            job["log"].append(str(msg))
            if "S&P" in str(msg):
                job["stage"] = "Fetching S&P CIQ IDs from GLEIF (identifiers only, about 10 minutes)"
        summary = build_universe_file(inh, lei2, rr, out, sp_ids=req.sp_ids, work_dir=OWNER_DIR, log=log)
        xl = pd.ExcelFile(out)
        decisions = len(xl.parse("LEI decisions")) if "LEI decisions" in xl.sheet_names else 0
        STATE["built"] = {"path": str(out), "file": out.name, "gleif_publish": Path(lei2).name,
                          "lei_decisions": decisions, "sp_ids": req.sp_ids,
                          "summary": [{"label": str(a).strip(), "value": str(b), "sub": str(a).startswith(" ")}
                                      for a, b in summary.values.tolist()]}
        return STATE["built"]
    return _start_job(run)


@app.get("/api/owner/build/file")
def owner_built_file():
    built = STATE["built"]
    if not built or not Path(built["path"]).exists():
        raise HTTPException(404, "Nothing built in this session yet.")
    return FileResponse(built["path"], filename=built["file"])


class PublishRequest(BaseModel):
    universe_file_id: Optional[str] = None   # an uploaded universe master; otherwise the one just built


@app.post("/api/owner/publish")
def owner_publish(req: PublishRequest):
    sh = _require_owner(_shared())
    if req.universe_file_id:
        path, info = _file(req.universe_file_id), {}
    elif STATE["built"]:
        path = Path(STATE["built"]["path"])
        info = {k: STATE["built"][k] for k in ("gleif_publish", "lei_decisions", "sp_ids")}
    else:
        raise HTTPException(400, "Build a universe master first, or choose a file to publish.")
    try:
        um = pd.read_excel(path, sheet_name="universe master", dtype=str)
        info["roles"] = um["role"].value_counts().to_dict()
    except Exception as e:
        raise HTTPException(400, f"That is not a universe master file ({e}).")
    try:
        manifest = sh.publish_reference(path, info)
    except OSError as e:
        raise HTTPException(500, f"Could not write to the shared folder: {e}")
    return manifest

# ---------- frontend ----------
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")
