"""Universe master file: every entity related to the in-house universe, built once, used for matching.

One row per identifier-bearing entity:
  coalition_id, coalition_name, role (group | subsidiary | successor | alternate), entity_name, other_names,
  lei, ciq_id, country, city, relationship, via_lei, lei_status, flag, lei_used

Build it (monthly is plenty) with scripts/build_universe_master.py from the GLEIF Golden Copy,
optionally enriched with GLEIF's free S&P CIQ Company ID <-> LEI mapping (identifiers only are sent).
The app then matches bank files against this file alone: no GLEIF files, no network.

LEI trust rules (a wrong LEI drags in a whole wrong group, e.g. an entity carrying Morgan Stanley's LEI):
  - in-house CIQ agrees with GLEIF's S&P ID for that LEI        -> confirmed (renames are fine)
  - they disagree and the in-house CIQ maps to another LEI        -> replaced by the CIQ's LEI
  - no S&P evidence and the name differs from every GLEIF name    -> suspect: not used for matching
  - no in-house LEI but the in-house CIQ maps to one               -> added from CIQ
"""
from __future__ import annotations

import pandas as pd

from .normalize import clean_id

MASTER_COLS = ["coalition_id", "coalition_name", "role", "entity_name", "other_names", "lei", "ciq_id",
               "country", "city", "relationship", "via_lei", "lei_status", "flag", "lei_used"]


def _ciq(x) -> str:
    x = clean_id(x)
    if not x:
        return ""
    return x if x.startswith("IQ") else f"IQ{x}"


def build_master(inh: pd.DataFrame, subset: pd.DataFrame, lei_info: dict, rr: pd.DataFrame | None,
                 names_agree, spg: dict | None = None) -> pd.DataFrame:
    """inh: inhouse_id, name, country, lei, ciq_id.  subset/lei_info: from gleif.scan_golden_copy.
    rr: active group relationships (child, parent, type) for the universe.  names_agree(mine, gleif_name) -> bool.
    spg: {"ciq2lei": {ciq_num: [(lei, name, status)]}, "lei2spg": {lei: [ciq_num]}} or None."""
    from .normalize import clean_name

    spg = spg or {"ciq2lei": {}, "lei2spg": {}}
    ciq2lei, lei2spg = spg["ciq2lei"], spg["lei2spg"]
    names_by_lei = {r.lei: r.names for r in subset.itertuples()}
    geo = {r.lei: (r.country, r.city) for r in subset.itertuples()}
    rows = []

    # ---- 1. groups (the in-house list itself), with LEI trust decisions ----
    group_lei: dict[str, str] = {}        # coalition_id -> LEI used for matching
    for r in inh.itertuples(index=False):
        cid, name, ciq = r.inhouse_id, r.name, _ciq(getattr(r, "ciq_id", ""))
        lei = clean_id(getattr(r, "lei", ""))
        ciq_num = ciq[2:] if ciq else ""
        from_ciq = [x for x in ciq2lei.get(ciq_num, []) if x[2] not in ("ANNULLED", "DUPLICATE")]
        flag, used, alternate = "", lei, ""
        bad_status = lei_info.get(lei, {}).get("registration_status") in ("RETIRED", "MERGED", "ANNULLED", "DUPLICATE")
        if lei:
            spg_ids = lei2spg.get(lei)
            gnames = [n for n in names_by_lei.get(lei, []) if n] or [lei_info.get(lei, {}).get("gleif_name", "")]
            name_ok = any(names_agree(clean_name(name), g) for g in gnames if g)
            # best LEI the CIQ points to: name agrees, then issued before lapsed
            cands = sorted(from_ciq, key=lambda x: (not names_agree(clean_name(name), x[1]),
                                                    {"ISSUED": 0, "LAPSED": 1}.get(x[2], 2)))
            best = cands[0] if cands and names_agree(clean_name(name), cands[0][1]) else None
            if ciq_num and ciq_num in (spg_ids or []) or any(x[0] == lei for x in from_ciq):
                flag = "LEI confirmed by CIQ" + ("" if name_ok else " (renamed)")
            elif best and (bad_status or not name_ok or lei not in lei_info):
                used = best[0]
                flag = f"LEI replaced via CIQ by {best[0]} ({best[1]}); list had {lei}"
            elif best:
                alternate = best[0]
                flag = f"LEI kept; CIQ also points to {best[0]} ({best[1]}), added as alternate"
            elif spg_ids and ciq_num:
                flag = "LEI and CIQ disagree; name " + ("agrees" if name_ok else "differs, LEI not used")
                if not name_ok:
                    used = ""
            elif not name_ok and lei in lei_info:
                flag, used = "suspect LEI: name differs and no CIQ evidence; not used", ""
            elif lei not in lei_info:
                flag = "LEI not in GLEIF"
        elif from_ciq:
            cands = sorted(from_ciq, key=lambda x: (not names_agree(clean_name(name), x[1]),
                                                    {"ISSUED": 0, "LAPSED": 1}.get(x[2], 2)))
            if names_agree(clean_name(name), cands[0][1]):
                used = cands[0][0]
                flag = f"LEI added via CIQ ({cands[0][1]})"
        if used:
            group_lei[cid] = used
        info = lei_info.get(used, {})
        c, city = geo.get(used, ("", ""))
        gn = names_by_lei.get(used, [])
        rows.append({"coalition_id": cid, "coalition_name": name, "role": "group",
                     "entity_name": gn[0] if gn else name, "other_names": "; ".join(gn[1:4]),
                     "lei": lei, "ciq_id": ciq, "country": c or getattr(r, "country", ""), "city": city,
                     "relationship": "itself", "via_lei": "", "lei_status": info.get("registration_status", ""),
                     "flag": flag, "lei_used": used})
        if alternate:
            rows.append({"coalition_id": cid, "coalition_name": name, "role": "alternate",
                         "entity_name": name, "other_names": "", "lei": alternate, "ciq_id": ciq,
                         "country": c or getattr(r, "country", ""), "city": city,
                         "relationship": "same group per CIQ link", "via_lei": used, "lei_status": "",
                         "flag": "", "lei_used": alternate})
        # successor of a retired/merged LEI
        succ = clean_id(info.get("successor_lei"))
        if succ:
            rows.append({"coalition_id": cid, "coalition_name": name, "role": "successor",
                         "entity_name": gn[0] if gn else name, "other_names": "", "lei": succ,
                         "ciq_id": _ciq((lei2spg.get(succ) or [""])[0]), "country": c, "city": city,
                         "relationship": f"successor of {used}", "via_lei": used, "lei_status": "",
                         "flag": "", "lei_used": succ})

    # ---- 2. subsidiaries of the groups whose LEI we trust ----
    lei2cid = {v: k for k, v in group_lei.items()}
    cid2name = dict(zip(inh["inhouse_id"], inh["name"]))
    if rr is not None and len(rr):
        prio = {"IS_DIRECTLY_CONSOLIDATED_BY": 0, "IS_INTERNATIONAL_BRANCH_OF": 1, "IS_ULTIMATELY_CONSOLIDATED_BY": 2}
        label = {"IS_DIRECTLY_CONSOLIDATED_BY": "direct", "IS_ULTIMATELY_CONSOLIDATED_BY": "ultimate",
                 "IS_INTERNATIONAL_BRANCH_OF": "branch"}
        hit = rr[rr["parent"].isin(lei2cid) & ~rr["child"].isin(lei2cid)].copy()
        hit["p"] = hit["type"].map(prio)
        kinds = hit.groupby("child")["type"].apply(lambda s: ", ".join(sorted({label[x] for x in s})))
        n_groups = hit.groupby("child")["parent"].nunique()
        for r in hit.sort_values(["child", "p"]).drop_duplicates("child").itertuples():
            cid = lei2cid[r.parent]
            gn = names_by_lei.get(r.child, [])
            c, city = geo.get(r.child, ("", ""))
            rows.append({"coalition_id": cid, "coalition_name": cid2name[cid], "role": "subsidiary",
                         "entity_name": gn[0] if gn else "", "other_names": "; ".join(gn[1:4]),
                         "lei": r.child, "ciq_id": _ciq((lei2spg.get(r.child) or [""])[0]),
                         "country": c, "city": city,
                         "relationship": kinds[r.child] + (" (under several of your groups)" if n_groups[r.child] > 1 else ""),
                         "via_lei": r.parent, "lei_status": "", "flag": "", "lei_used": r.child})
    return pd.DataFrame(rows, columns=MASTER_COLS)


def reference_inputs(master: pd.DataFrame):
    """Turn a universe master file into build_reference() inputs: (inhouse, gleif_subset, extra_ciq)."""
    m = master.fillna("").astype(str)
    g = m[m["role"] == "group"]
    inh = pd.DataFrame({"inhouse_id": g["coalition_id"], "name": g["coalition_name"], "country": g["country"],
                        "city": g["city"], "lei": g["lei_used"], "ciq_id": g["ciq_id"]})
    rows = []
    group_used = dict(zip(g["coalition_id"], g["lei_used"]))
    for r in m.itertuples(index=False):
        names = [r.entity_name] + [n.strip() for n in r.other_names.split(";") if n.strip()]
        names = [n for n in names if n]
        if r.role == "group" and r.lei_used:
            rows.append({"lei": r.lei_used, "parent_lei": "", "names": names, "country": r.country, "city": r.city})
        elif r.role in ("subsidiary", "successor", "alternate") and r.lei_used and group_used.get(r.coalition_id):
            rows.append({"lei": r.lei_used, "parent_lei": group_used[r.coalition_id], "names": names,
                         "country": r.country, "city": r.city})
    extra_ciq = {r.ciq_id: (r.coalition_id, "self" if r.role in ("group", "alternate") else "subsidiary")
                 for r in m.itertuples(index=False) if r.ciq_id}
    return inh, pd.DataFrame(rows, columns=["lei", "parent_lei", "names", "country", "city"]), extra_ciq
