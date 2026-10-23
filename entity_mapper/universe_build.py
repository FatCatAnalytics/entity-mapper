"""Build the universe master file (owner task, monthly): GLEIF scan, optional S&P CIQ IDs, LEI trust rules.

Used by scripts/build_universe_master.py and by the app's Owner screen. The optional S&P step calls the
GLEIF API with IDENTIFIERS ONLY (your LEIs and CIQ numbers; ~500 requests, ~10 min, resumable).
"""
from __future__ import annotations

import json
import pickle
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from .gleif import GROUP_RELATIONSHIPS, scan_golden_copy
from .leicheck import lei_check, names_agree
from .normalize import clean_id, clean_name
from .universe import build_master

API = "https://api.gleif.org/api/v1/lei-records"

def api_get(params):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(8):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            time.sleep(30 if e.code == 429 else 5)
        except Exception:
            time.sleep(3 + attempt * 2)
    raise RuntimeError(f"GLEIF API kept failing: {url[:120]}")


def api_paged(key, ids, fields):
    out, page = [], 1
    while True:
        d = api_get({f"filter[{key}]": ",".join(ids), "page[size]": 200, "page[number]": page,
                     "fields[lei-records]": fields})
        time.sleep(1.05)  # GLEIF allows 60 requests a minute
        out += d["data"]
        if page >= d["meta"]["pagination"]["lastPage"]:
            return out
        page += 1


def fetch_sp_ids(ciqs, leis, state_path, log=print):
    """GLEIF's S&P CIQ Company ID <-> LEI mapping, for our IDs only. Resumable."""
    st = (pickle.loads(Path(state_path).read_bytes()) if Path(state_path).exists()
          else {"ciq2lei": {}, "lei2spg": {}, "doneA": set(), "doneB": set()})
    for i in range(0, len(ciqs), 50):
        if i in st["doneA"]:
            continue
        for r in api_paged("spglobal", ciqs[i:i + 50], "spglobal,entity,registration"):
            for s in r["attributes"].get("spglobal") or []:
                st["ciq2lei"].setdefault(s, []).append((r["id"], r["attributes"]["entity"]["legalName"]["name"],
                                                        r["attributes"]["registration"]["status"]))
        st["doneA"].add(i)
        Path(state_path).write_bytes(pickle.dumps(st))
        log(f"  CIQ -> LEI {min(i + 50, len(ciqs))}/{len(ciqs)}")
    for i in range(0, len(leis), 100):
        if i in st["doneB"]:
            continue
        for r in api_paged("lei", leis[i:i + 100], "spglobal"):
            st["lei2spg"][r["id"]] = r["attributes"].get("spglobal") or []
        st["doneB"].add(i)
        Path(state_path).write_bytes(pickle.dumps(st))
        if (i // 100) % 25 == 0:
            log(f"  LEI -> CIQ {min(i + 100, len(leis))}/{len(leis)}")
    return {"ciq2lei": st["ciq2lei"], "lei2spg": st["lei2spg"]}


def build_universe_file(inh: pd.DataFrame, lei2, rr, out, sp_ids: bool = False, sp_cache=None,
                        work_dir=None, log=print) -> pd.DataFrame:
    """inh: inhouse_id, name, country, lei, ciq_id. lei2 / rr: Golden Copy files (.csv or .csv.zip).
    Writes the universe master .xlsx to `out` and returns its Summary sheet."""
    lei2, rr, out = Path(lei2).expanduser(), Path(rr).expanduser(), Path(out)
    work_dir = Path(work_dir) if work_dir else out.parent
    inh = inh.reindex(columns=["inhouse_id", "name", "country", "lei", "ciq_id"]).fillna("").astype(str)
    inh["lei"] = inh["lei"].map(clean_id)
    inh["ciq_id"] = inh["ciq_id"].map(clean_id)

    t = time.time()
    log("Scanning the Golden Copy (one pass)...")
    scan = scan_golden_copy(lei2, set(inh["lei"]), {clean_name(n) for n in inh["name"]},
                            rr_path=rr, cache_dir=work_dir / ".gleif_cache")
    log(f"  {len(scan['subset']):,} GLEIF records kept in {time.time() - t:.0f}s")

    rr = pd.read_csv(rr, dtype=str,
                     usecols=["Relationship.StartNode.NodeID", "Relationship.EndNode.NodeID",
                              "Relationship.RelationshipType", "Relationship.RelationshipStatus"])
    rr.columns = ["child", "parent", "type", "status"]
    rr = rr[(rr["status"] == "ACTIVE") & rr["type"].isin(GROUP_RELATIONSHIPS)]

    spg = None
    if sp_cache:
        spg = pickle.loads(Path(sp_cache).read_bytes())
    elif sp_ids:
        log("Fetching S&P CIQ IDs from GLEIF (identifiers only)...")
        ciqs = [c[2:] if c.startswith("IQ") else c for c in inh["ciq_id"] if c]
        leis = sorted(set(inh["lei"]) - {""} | set(scan["subset"]["lei"]))
        spg = fetch_sp_ids(ciqs, leis, work_dir / ".sp_ids_state.pkl", log)

    names_by_lei = {r.lei: r.names for r in scan["subset"].itertuples() if not r.parent_lei}
    check = lei_check(inh, scan["lei_info"], names_by_lei)
    master = build_master(inh, scan["subset"], scan["lei_info"], rr[rr["parent"].isin(set(scan["subset"]["lei"]))],
                          names_agree, spg)

    groups = master[master.role == "group"]
    flags = groups["flag"].fillna("")
    summary = pd.DataFrame([
        ["Your entities", len(groups)],
        ["  with an LEI used for matching", int((groups.lei_used != "").sum())],
        ["  LEI confirmed by CIQ", int(flags.str.startswith("LEI confirmed").sum())],
        ["  LEI replaced (CIQ points to another LEI)", int(flags.str.startswith("LEI replaced").sum())],
        ["  LEI added from CIQ (had none)", int(flags.str.startswith("LEI added").sum())],
        ["  LEI not used (suspect / disagreeing)", int(((groups.lei != "") & (groups.lei_used == "")).sum())],
        ["Subsidiaries", int((master.role == "subsidiary").sum())],
        ["  with a CIQ ID", int(((master.role == "subsidiary") & (master.ciq_id != "")).sum())],
        ["Successor LEIs", int((master.role == "successor").sum())],
        ["Total identifiers pointing to your universe", int((master.lei_used != "").sum())],
        ["GLEIF publish used", lei2.name],
        ["S&P CIQ mapping", "yes" if spg else "no"],
    ], columns=["", "count"])
    name_countries = pd.DataFrame([{"name": k, "countries": ",".join(sorted(v))}
                                   for k, v in scan["name_index"].items()])

    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        master.to_excel(xw, sheet_name="universe master", index=False)
        master[(master.flag != "") & (master.flag != "LEI confirmed by CIQ")].to_excel(
            xw, sheet_name="LEI decisions", index=False)
        check[check.severity.isin(["action", "check"])].to_excel(xw, sheet_name="LEI check", index=False)
        name_countries.to_excel(xw, sheet_name="name countries", index=False)
    log(summary.to_string(index=False))
    log(f"Wrote {out} in {time.time() - t:.0f}s")

    return summary
