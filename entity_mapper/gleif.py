"""Local GLEIF Golden Copy handling.

Key design point: we never match bank names against all ~3M GLEIF entities.
A GLEIF hit is only useful if it leads to an in-house record, so we keep just:
  * GLEIF records whose LEI is in the in-house list (adds legal/other/previous
    names as extra match variants for that in-house entity), and
  * their direct/ultimate children (so a bank's subsidiary can map to an
    in-house parent, flagged as relation='subsidiary').
That shrinks the reference to roughly the size of the in-house list, reading
the multi-GB Golden Copy once per refresh, in chunks, fully offline.

Accepted inputs
  * Golden Copy LEI-CDF CSV (Level 1) and optional RR-CDF CSV (Level 2), or
  * a simple CSV with columns: lei, legal_name, other_names (';'-separated),
    country, city, parent_lei
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .normalize import clean_id

CHUNK = 250_000
GROUP_RELATIONSHIPS = {"IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY",
                       "IS_INTERNATIONAL_BRANCH_OF"}


def _s(v) -> str:
    """Blank for None/NaN, else a stripped string."""
    return v.strip() if isinstance(v, str) else ""


def _find(cols, *candidates):
    low = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def read_children(rr_path: str | Path, parent_leis: set[str]) -> dict[str, str]:
    """child LEI -> in-house parent LEI, from the Level 2 relationship file."""
    out: dict[str, str] = {}
    head = pd.read_csv(rr_path, nrows=0)
    start = _find(head.columns, "Relationship.StartNode.NodeID", "child_lei", "start_lei")
    end = _find(head.columns, "Relationship.EndNode.NodeID", "parent_lei", "end_lei")
    rtype = _find(head.columns, "Relationship.RelationshipType", "relationship_type")
    status = _find(head.columns, "Relationship.RelationshipStatus")
    usecols = [c for c in (start, end, rtype, status) if c]
    for chunk in pd.read_csv(rr_path, usecols=usecols, dtype=str, chunksize=CHUNK):
        if status:
            chunk = chunk[chunk[status].fillna("ACTIVE").str.upper() == "ACTIVE"]
        if rtype:  # ownership only: a fund managed by an asset manager is NOT part of its group
            chunk = chunk[chunk[rtype].fillna("IS_DIRECTLY_CONSOLIDATED_BY").isin(GROUP_RELATIONSHIPS)]
        hit = chunk[chunk[end].isin(parent_leis)]
        # Prefer the direct parent when a child has both direct and ultimate links.
        if rtype:
            hit = hit.sort_values(rtype)  # IS_DIRECTLY_* sorts before IS_ULTIMATELY_*
        for child, parent in zip(hit[start], hit[end]):
            out.setdefault(child, parent)
    return out


def load_gleif_subset(l1_path: str | Path, inhouse_leis: set[str],
                      rr_path: str | Path | None = None) -> pd.DataFrame:
    """Return rows: lei, parent_lei ('' if the LEI itself is in-house), names (list), country, city."""
    inhouse_leis = {clean_id(x) for x in inhouse_leis if clean_id(x)}
    children = read_children(rr_path, inhouse_leis) if rr_path else {}
    wanted = inhouse_leis | set(children)

    head = pd.read_csv(l1_path, nrows=0)
    cols = list(head.columns)
    lei_c = _find(cols, "LEI", "lei")
    name_c = _find(cols, "Entity.LegalName", "legal_name", "name")
    country_c = _find(cols, "Entity.LegalAddress.Country", "country")
    city_c = _find(cols, "Entity.LegalAddress.City", "city")
    parent_c = _find(cols, "parent_lei")
    other_simple = _find(cols, "other_names")
    other_cdf = [c for c in cols if c.startswith("Entity.OtherEntityNames.OtherEntityName")
                 and not c.endswith(("xmllang", "type"))]
    other_cdf += [c for c in cols if c.startswith("Entity.TransliteratedOtherEntityNames")
                  and not c.endswith(("xmllang", "type"))]
    usecols = [c for c in [lei_c, name_c, country_c, city_c, parent_c, other_simple, *other_cdf] if c]

    rows = []
    for chunk in pd.read_csv(l1_path, usecols=usecols, dtype=str, chunksize=CHUNK):
        keep = chunk[lei_c].isin(wanted)
        if parent_c:  # simple format declares children inline
            keep |= chunk[parent_c].isin(inhouse_leis)
        chunk = chunk[keep]
        for rec in chunk.to_dict("records"):
            names = [rec.get(name_c)]
            if other_simple and isinstance(rec.get(other_simple), str):
                names += rec[other_simple].split(";")
            names += [rec.get(c) for c in other_cdf]
            names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
            lei = rec[lei_c]
            parent = ""
            if lei not in inhouse_leis:
                parent = children.get(lei, "")
                if not parent and parent_c:
                    parent = clean_id(rec.get(parent_c))
            rows.append({
                "lei": lei,
                "parent_lei": parent or "",
                "names": names,
                "country": _s(rec.get(country_c)),
                "city": _s(rec.get(city_c)),
            })
    return pd.DataFrame(rows, columns=["lei", "parent_lei", "names", "country", "city"])


def name_country_index(l1_path: str | Path, target_names: set[str]) -> dict[str, set[str]]:
    """For each cleaned name in `target_names` (the in-house variants), the set of countries
    in which GLEIF has a legal entity with that same cleaned name.

    Used as a safety net for name-only bank files: 'Foo Holdings Limited' registered in both
    GB and IE cannot be resolved without a country, so it goes to review instead of auto.
    Reads the whole Golden Copy once; only names sharing a first word with an in-house
    name are cleaned, which keeps this to minutes rather than hours.
    """
    from .normalize import clean_name, normalize_country

    first_words = {n.split()[0] for n in target_names if n}
    head = pd.read_csv(l1_path, nrows=0)
    cols = list(head.columns)
    name_c = _find(cols, "Entity.LegalName", "legal_name", "name")
    country_c = _find(cols, "Entity.LegalAddress.Country", "country")
    status_c = _find(cols, "Entity.EntityStatus")
    usecols = [c for c in (name_c, country_c, status_c) if c]
    out: dict[str, set[str]] = {}
    for chunk in pd.read_csv(l1_path, usecols=usecols, dtype=str, chunksize=CHUNK):
        if status_c:
            chunk = chunk[chunk[status_c].fillna("ACTIVE").str.upper() == "ACTIVE"]
        names = chunk[name_c].fillna("")
        fw = names.str.lower().str.split(n=1).str[0].fillna("")
        chunk = chunk[fw.isin(first_words)]
        for n, c in zip(chunk[name_c], chunk[country_c]):
            cn = clean_name(n)
            if cn in target_names:
                out.setdefault(cn, set()).add(normalize_country(c) or str(c))
    return out



def scan_golden_copy(l1_path, inhouse_leis: set[str], target_names: set[str],
                     rr_path=None, cache_dir=None) -> dict:
    """One pass over the Level 1 file (CSV or .csv.zip, read in chunks, never unpacked).

    Returns
      subset:     GLEIF rows for in-house LEIs and their subsidiaries (as load_gleif_subset)
      name_index: cleaned in-house name -> countries where GLEIF has an ACTIVE entity with that name
      lei_info:   per in-house LEI: GLEIF legal name, country, entity/registration status, successor
    Cached by file name + in-house LEIs + names, so rebuilding the reference is instant.
    """
    import hashlib
    import pickle
    from .normalize import clean_name, normalize_country

    l1_path = Path(l1_path)
    inhouse_leis = {clean_id(x) for x in inhouse_leis if clean_id(x)}
    key = hashlib.sha1((l1_path.name + str(rr_path and Path(rr_path).name) + "|".join(sorted(inhouse_leis))
                        + "|".join(sorted(target_names))).encode()).hexdigest()[:16]
    cache = Path(cache_dir) / f"gleif_{key}.pkl" if cache_dir else None
    if cache and cache.exists():
        return pickle.loads(cache.read_bytes())

    children = read_children(rr_path, inhouse_leis) if rr_path else {}
    wanted = inhouse_leis | set(children)
    first_words = {n.split()[0] for n in target_names if n}

    head = pd.read_csv(l1_path, nrows=0)
    cols = list(head.columns)
    lei_c = _find(cols, "LEI", "lei")
    name_c = _find(cols, "Entity.LegalName", "legal_name", "name")
    country_c = _find(cols, "Entity.LegalAddress.Country", "country")
    city_c = _find(cols, "Entity.LegalAddress.City", "city")
    estat_c = _find(cols, "Entity.EntityStatus")
    rstat_c = _find(cols, "Registration.RegistrationStatus")
    succ_c = next((c for c in cols if c.endswith("SuccessorLEI")), None)
    parent_c = _find(cols, "parent_lei")
    other_simple = _find(cols, "other_names")
    other_cdf = [c for c in cols if (c.startswith("Entity.OtherEntityNames.OtherEntityName")
                                     or c.startswith("Entity.TransliteratedOtherEntityNames"))
                 and not c.endswith(("xmllang", "type"))]
    usecols = [c for c in [lei_c, name_c, country_c, city_c, estat_c, rstat_c, succ_c, parent_c,
                           other_simple, *other_cdf] if c]

    rows, index, info = [], {}, {}
    for chunk in pd.read_csv(l1_path, usecols=usecols, dtype=str, chunksize=CHUNK):
        active = chunk[estat_c].fillna("ACTIVE").str.upper().eq("ACTIVE") if estat_c else True
        # 1. name index: only names sharing a first word with an in-house name are cleaned
        fw = chunk[name_c].fillna("").str.lower().str.split(n=1).str[0].fillna("")
        cand = chunk[fw.isin(first_words) & active]
        for n, c in zip(cand[name_c], cand[country_c]):
            cn = clean_name(n)
            if cn in target_names:
                index.setdefault(cn, set()).add(normalize_country(c) or str(c))
        # 2. subset: in-house LEIs and their subsidiaries
        keep = chunk[lei_c].isin(wanted)
        if parent_c:
            keep |= chunk[parent_c].isin(inhouse_leis)
        for rec in chunk[keep].to_dict("records"):
            lei = rec[lei_c]
            names = [rec.get(name_c)]
            if other_simple and isinstance(rec.get(other_simple), str):
                names += rec[other_simple].split(";")
            names += [rec.get(c) for c in other_cdf]
            names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
            parent = ""
            if lei not in inhouse_leis:
                parent = children.get(lei, "") or (clean_id(rec.get(parent_c)) if parent_c else "")
            rows.append({"lei": lei, "parent_lei": parent, "names": names,
                         "country": _s(rec.get(country_c)), "city": _s(rec.get(city_c))})
            if lei in inhouse_leis:
                info[lei] = {"gleif_name": _s(rec.get(name_c)), "gleif_country": _s(rec.get(country_c)),
                             "entity_status": _s(rec.get(estat_c)), "registration_status": _s(rec.get(rstat_c)),
                             "successor_lei": _s(rec.get(succ_c))}
    # A retired/merged LEI with a successor: the successor LEI identifies the same business.
    by_lei = {r["lei"]: r for r in rows if not r["parent_lei"]}
    for lei, i in info.items():
        succ = clean_id(i.get("successor_lei"))
        if succ and succ not in wanted and lei in by_lei:
            rows.append({**by_lei[lei], "lei": succ, "parent_lei": lei, "successor": True})
    out = {"subset": pd.DataFrame(rows, columns=["lei", "parent_lei", "names", "country", "city"]),
           "name_index": index, "lei_info": info, "children": len(children)}
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps(out))
    return out
