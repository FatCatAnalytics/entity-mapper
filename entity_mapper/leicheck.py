"""LEI quality check: compare each in-house LEI with GLEIF."""
from __future__ import annotations

import re

import pandas as pd
from unidecode import unidecode

from .normalize import clean_name

_STOP = {"of", "and", "the", "de", "des", "du", "la", "le", "y", "e", "&", "und"}


def names_agree(mine: str, gleif_name: str) -> bool:
    """Similar wording, or the in-house name is an acronym of the GLEIF name (BMW, SAIL, FEMSA)."""
    from rapidfuzz import fuzz
    theirs = clean_name(gleif_name)
    if max(fuzz.token_set_ratio(mine, theirs), fuzz.partial_ratio(mine, theirs)) >= 70:
        return True
    squashed = mine.replace(" ", "")
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", unidecode(gleif_name).lower()).split() if w not in _STOP]
    initials = "".join(w[0] for w in words)
    return len(squashed) >= 2 and initials.startswith(squashed)


def lei_check(inh: pd.DataFrame, info: dict, names_by_lei: dict | None = None) -> pd.DataFrame:
    """Grade each in-house LEI against GLEIF.

    action   = the LEI should be fixed (checksum, not found, annulled, retired/merged -> use successor,
               GLEIF duplicate, same LEI on two in-house rows)
    check    = worth a look (entity inactive, name differs from every GLEIF name for that LEI:
               often a rename, sometimes the wrong LEI)
    info     = lapsed (not renewed): the LEI still identifies the entity; fine for matching
    """
    from rapidfuzz import fuzz
    from entity_mapper.normalize import clean_id, lei_is_valid
    names_by_lei = names_by_lei or {}
    lei_col = (inh["lei"] if "lei" in inh else pd.Series([""] * len(inh), index=inh.index)).map(clean_id)
    counts = lei_col[lei_col != ""].value_counts()
    rows = []
    for (_, r), lei in zip(inh.iterrows(), lei_col):
        if not lei:
            continue
        g = info.get(lei, {})
        action, check, note = [], [], []
        if not lei_is_valid(lei):
            action.append("checksum fails")
        elif not g:
            action.append("not in GLEIF")
        else:
            reg = g["registration_status"]
            if reg in ("ANNULLED", "DUPLICATE"):
                action.append(f"registration {reg}")
            elif reg in ("RETIRED", "MERGED"):
                action.append(f"registration {reg}")
            elif reg == "LAPSED":
                note.append("lapsed (not renewed)")
            if g["successor_lei"]:
                action.append(f"use successor {g['successor_lei']}")
            if g["entity_status"] and g["entity_status"] != "ACTIVE" and reg not in ("RETIRED", "MERGED"):
                check.append(f"entity {g['entity_status']}")
            gnames = [n for n in (names_by_lei.get(lei) or [g["gleif_name"]]) if n]
            latin = [n for n in gnames if re.search(r"[A-Za-z]{2}", n)]
            mine = clean_name(r["name"])
            if not latin:
                note.append("GLEIF name not in Latin script (not compared)")
            elif not any(names_agree(mine, n) for n in latin):
                check.append("name differs from GLEIF (rename or wrong LEI?)")
        if counts.get(lei, 0) > 1:
            action.append("same LEI on another in-house row")
        severity = "action" if action else "check" if check else "info" if note else ""
        rows.append({"severity": severity, "inhouse_id": r.get("inhouse_id", ""), "name": r["name"],
                     "lei": lei, "gleif_name": g.get("gleif_name", ""), "gleif_country": g.get("gleif_country", ""),
                     "registration_status": g.get("registration_status", ""),
                     "issue": "; ".join(action + check + note)})
    order = {"action": 0, "check": 1, "info": 2, "": 3}
    return pd.DataFrame(rows).sort_values("severity", key=lambda x: x.map(order)).reset_index(drop=True)


