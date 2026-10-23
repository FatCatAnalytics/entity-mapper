"""Period mapping: prior delivery → master → parent/group → universe → write-back.

Tiers (first confident hit wins; later signals can flag conflicts):
  delivered_id / delivered_name   prior Mapped delivery
  master_exact                    Granular Name in the manual master
  capiq                           bank id is a CapIQ id in the universe (optional)
  parent_unique                   all Mapped siblings under the bank parent → one Coalition
  parent_name                     bank parent name equals a Coalition name
  master_contain / master_fuzzy   near-misses against the master
  universe:*                      published universe (+ GLEIF bridges)
  parent_majority / parent_thin   review candidates from sibling history
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz, process

from .matcher import BANK_COLS, Config, Reference, match
from .normalize import clean_name
from .shared import now_utc

MASTER_OUT_COLS = [
    "Granular Name", "Status", "Coalition ID", "Coalition Names", "Mapped At", "Source",
]

_STOP = {
    "the", "and", "of", "co", "company", "ltd", "limited", "plc", "sa", "nv", "inc", "corp",
    "group", "holding", "holdings", "sas", "sarl", "gmbh", "ag", "bv", "llc", "spa", "srl",
    "societe", "ste", "soc", "ets", "des", "de", "du", "la", "le", "les", "et",
}


@dataclass
class PeriodOptions:
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


@dataclass
class PeriodMaps:
    by_id: dict[str, tuple[str, str]]
    by_name: dict[str, tuple[str, str]]
    master_exact: dict[str, tuple[str, str]]
    master_names: dict[str, str]
    parent_groups: dict[str, Counter] = field(default_factory=dict)
    coalition_by_name: dict[str, str] = field(default_factory=dict)
    capiq: dict[str, str] = field(default_factory=dict)
    master_keys: list[str] = field(default_factory=list)
    master_tokens: dict[str, list[int]] = field(default_factory=dict)
    master_key_to_hit: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass
class ParentSignal:
    cid: str
    kind: str  # unique | majority | thin
    n: int
    total: int

    @property
    def share(self) -> float:
        return self.n / self.total if self.total else 0.0

    @property
    def strong(self) -> bool:
        return self.kind in ("unique", "majority")

    def label(self) -> str:
        if self.kind == "majority":
            return f"{self.n}/{self.total} Mapped siblings"
        if self.kind == "thin":
            return f"only {self.total} Mapped sibling{'s' if self.total != 1 else ''}"
        return f"all {self.total} Mapped siblings"


def norm_capiq(x) -> str:
    s = str(x or "").strip().upper().replace(" ", "")
    if not s or s == "NAN":
        return ""
    if s.startswith("IQ"):
        return s
    return f"IQ{s}" if s.isdigit() else s


def _s(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _is_mapped(status: str, coalition_id: str) -> bool:
    return bool(coalition_id) and status.lower() in ("mapped", "map", "yes", "y", "1", "true")


def _sig_tokens(key: str) -> list[str]:
    return [t for t in key.split() if t not in _STOP and len(t) > 2]


def _pick(columns, candidates: list[str]) -> str | None:
    lower = {c.lower().strip(): c for c in columns}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    for c in columns:
        cl = c.lower().strip()
        if any(cand in cl for cand in candidates):
            return c
    return None


def _col(df: pd.DataFrame, mapping: dict, field: str, candidates: list[str]) -> str | None:
    return mapping.get(field) or _pick(df.columns, candidates)


def prepare_master(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    name_c = _col(df, mapping, "name", ["granular name", "name", "entity name", "client_lename"])
    id_c = _col(df, mapping, "inhouse_id", ["coalition id", "inhouse_id", "coalition_id"])
    names_c = _col(df, mapping, "coalition_names", ["coalition names", "coalition_name", "group"])
    status_c = _col(df, mapping, "status", ["status", "mapped"])
    at_c = _col(df, mapping, "mapped_at", ["mapped at", "mapped_at", "timestamp"])
    src_c = _col(df, mapping, "source", ["source", "mapped by", "mapped_by"])

    out = pd.DataFrame({
        "Granular Name": df[name_c].map(_s) if name_c else "",
        "Status": df[status_c].map(_s) if status_c else "",
        "Coalition ID": df[id_c].map(_s) if id_c else "",
        "Coalition Names": df[names_c].map(_s) if names_c else "",
        "Mapped At": df[at_c].map(_s) if at_c else "",
        "Source": df[src_c].map(_s) if src_c else "",
    })
    blank = out["Status"] == ""
    out.loc[blank & out["Coalition ID"].ne(""), "Status"] = "Mapped"
    out.loc[blank & out["Coalition ID"].eq(""), "Status"] = "Not Mapped"
    return out


def prepare_delivered(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    name_c = _col(df, mapping, "name", ["client_lename", "lename", "name", "legal_name"])
    id_c = mapping.get("ciq_id") or mapping.get("bank_row_id") or _pick(
        df.columns, ["client_leid", "leid", "ciq_id", "companyid"])
    coal_c = _col(df, mapping, "inhouse_id", ["coalition id", "coalition_id", "inhouse_id"])
    names_c = _col(df, mapping, "coalition_names", ["coalition names", "coalition_name"])
    status_c = _col(df, mapping, "status", ["status"])
    grp_c = _pick(df.columns, ["client_corigrpfullname", "client_corigrpname", "corigrpfullname", "group_name"])
    grp_id_c = _pick(df.columns, ["client_corigrpcomid", "corigrpcomid"])
    return pd.DataFrame({
        "client_leid": df[id_c].map(_s) if id_c else "",
        "client_lename": df[name_c].map(_s) if name_c else "",
        "Status": df[status_c].map(_s) if status_c else "",
        "Coalition ID": df[coal_c].map(_s) if coal_c else "",
        "Coalition Names": df[names_c].map(_s) if names_c else "",
        "client_corigrpname": df[grp_c].map(_s) if grp_c else "",
        "client_corigrpcomid": df[grp_id_c].map(_s) if grp_id_c else "",
    })


def prepare_received(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    name_c = _col(df, mapping, "name", ["client_lename", "lename", "name", "legal_name"])
    id_c = mapping.get("ciq_id") or mapping.get("bank_row_id") or _pick(
        df.columns, ["client_leid", "leid", "ciq_id", "companyid"])
    grp_c = _pick(df.columns, ["client_corigrpfullname", "client_corigrpname", "corigrpfullname", "group_name"])
    grp_id_c = _pick(df.columns, ["client_corigrpcomid", "corigrpcomid"])
    country_c = _col(df, mapping, "country", ["country", "country_code"])
    return pd.DataFrame({
        "bank_row_id": df[id_c].map(_s) if id_c else (df.index + 1).astype(str),
        "name": df[name_c].map(_s) if name_c else "",
        "ciq_id": df[id_c].map(_s) if id_c else "",
        "country": df[country_c].map(_s) if country_c else "",
        "city": "",
        "lei": "",
        "client_corigrpname": df[grp_c].map(_s) if grp_c else "",
        "client_corigrpcomid": df[grp_id_c].map(_s) if grp_id_c else "",
    })


def build_maps(delivered: pd.DataFrame, master: pd.DataFrame,
               ref: Reference | None = None, universe: pd.DataFrame | None = None) -> PeriodMaps:
    by_id: dict[str, tuple[str, str]] = {}
    by_name: dict[str, tuple[str, str]] = {}
    parent_groups: dict[str, Counter] = defaultdict(Counter)
    for r in delivered.to_dict("records"):
        cid = _s(r.get("Coalition ID"))
        if not _is_mapped(_s(r.get("Status")), cid):
            continue
        cnames = _s(r.get("Coalition Names"))
        leid = _s(r.get("client_leid"))
        lename = _s(r.get("client_lename"))
        if leid and leid not in by_id:
            by_id[leid] = (cid, cnames)
        key = clean_name(lename)
        if key and key not in by_name:
            by_name[key] = (cid, cnames)
        pid = _s(r.get("client_corigrpcomid"))
        if pid:
            parent_groups[pid][cid] += 1

    master_exact: dict[str, tuple[str, str]] = {}
    master_names: dict[str, str] = {}
    coalition_by_name: dict[str, str] = {}
    for r in master.to_dict("records"):
        cid = _s(r.get("Coalition ID"))
        cnames = _s(r.get("Coalition Names"))
        gname = _s(r.get("Granular Name"))
        if cid:
            master_names.setdefault(cid, cnames or gname)
            ck = clean_name(cnames)
            if ck:
                coalition_by_name.setdefault(ck, cid)
        if _is_mapped(_s(r.get("Status")), cid):
            key = clean_name(gname)
            if key and key not in master_exact:
                master_exact[key] = (cid, cnames)

    capiq: dict[str, str] = {}
    if universe is not None and len(universe):
        for r in universe.to_dict("records"):
            cid = _s(r.get("coalition_id"))
            if not cid:
                continue
            ck = clean_name(_s(r.get("coalition_name")))
            if ck:
                coalition_by_name.setdefault(ck, cid)
            master_names.setdefault(cid, _s(r.get("coalition_name")))
            iq = norm_capiq(r.get("ciq_id"))
            if iq:
                capiq.setdefault(iq, cid)
    elif ref is not None:
        for cid, name in ref.inhouse["name"].items():
            ck = clean_name(name)
            if ck:
                coalition_by_name.setdefault(ck, str(cid))
            master_names.setdefault(str(cid), name)
        for iq, cid in ref.ciq_map.items():
            k = norm_capiq(iq)
            if k:
                capiq.setdefault(k, str(cid))

    master_keys = list(master_exact.keys())
    master_tokens: dict[str, list[int]] = defaultdict(list)
    for pos, key in enumerate(master_keys):
        for t in set(_sig_tokens(key)):
            master_tokens[t].append(pos)

    return PeriodMaps(
        by_id=by_id, by_name=by_name, master_exact=master_exact, master_names=master_names,
        parent_groups=dict(parent_groups), coalition_by_name=coalition_by_name, capiq=capiq,
        master_keys=master_keys, master_tokens=dict(master_tokens), master_key_to_hit=master_exact,
    )


def _parent_signal(maps: PeriodMaps, parent_id: str, opts: PeriodOptions) -> ParentSignal | None:
    if not parent_id:
        return None
    counter = maps.parent_groups.get(parent_id)
    if not counter:
        return None
    total = sum(counter.values())
    cid, n = counter.most_common(1)[0]
    if len(counter) == 1:
        kind = "unique" if total >= opts.parent_min_siblings else "thin"
        return ParentSignal(cid, kind, n, total)
    if total >= opts.parent_min_siblings and n / total >= opts.parent_majority_share:
        return ParentSignal(cid, "majority", n, total)
    return None


def _master_contain(maps: PeriodMaps, key: str) -> tuple[tuple[str, str], str, int] | None:
    toks = _sig_tokens(key)
    if not toks:
        return None
    cands: set[int] = set()
    for t in toks[:4]:
        cands.update(maps.master_tokens.get(t, ())[:200])
    hits: dict[str, tuple[str, int]] = {}
    for pos in cands:
        other = maps.master_keys[pos]
        if other == key:
            continue
        a, b = set(toks), set(_sig_tokens(other))
        if not a or not b:
            continue
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        if len(short) >= 2 and short <= long_:
            cid, _ = maps.master_key_to_hit[other]
            prev = hits.get(cid)
            if prev is None or len(short) > prev[1]:
                hits[cid] = (other, len(short))
    if len(hits) != 1:
        return None
    cid, (via, n) = next(iter(hits.items()))
    return (cid, maps.master_names.get(cid, "")), via, n


def _master_fuzzy(maps: PeriodMaps, key: str, opts: PeriodOptions):
    toks = _sig_tokens(key)
    if not toks:
        return None
    cands: set[int] = set()
    for t in toks[:4]:
        cands.update(maps.master_tokens.get(t, ())[:300])
    if not cands:
        return None
    choices = [maps.master_keys[p] for p in cands]
    top = process.extract(key, choices, scorer=fuzz.token_sort_ratio, limit=3)
    if not top or top[0][1] < opts.master_fuzzy_review:
        return None
    best, score = top[0][0], float(top[0][1])
    cid, cnames = maps.master_key_to_hit[best]
    runner = next((float(s) for c, s, _ in top[1:] if maps.master_key_to_hit[c][0] != cid), 0.0)
    return (cid, cnames or maps.master_names.get(cid, "")), best, score, score - runner


def _fill_hit(base: dict, hit: tuple[str, str], tier: str, evidence: str, score, status: str,
              matched_via: str = "", relation: str = "self") -> dict:
    cid, cnames = hit
    out = dict(base)
    out.update({
        "Status": "Mapped" if status == "auto" else "Not Mapped",
        "Coalition ID": cid, "Coalition Names": cnames,
        "inhouse_id": cid, "inhouse_name": cnames,
        "tier": tier, "evidence": evidence, "score": score,
        "matched_via": matched_via or tier, "relation": relation, "status": status,
    })
    return out


def _set_review(row: dict, hit: tuple[str, str], tier: str, evidence: str, score) -> None:
    row.update(_fill_hit(row, hit, tier, evidence, score, "review"))


def _add_candidate(row: dict, cid: str, name: str, score) -> None:
    if not cid or cid == row.get("inhouse_id"):
        return
    for k in (2, 3):
        cur = row.get(f"inhouse_id_{k}", "")
        if cur == cid:
            return
        if not cur:
            row[f"inhouse_id_{k}"] = cid
            row[f"inhouse_name_{k}"] = name
            row[f"score_{k}"] = score
            return


def period_match(
    received: pd.DataFrame,
    delivered: pd.DataFrame,
    master: pd.DataFrame,
    ref: Reference | None = None,
    cfg: Config | None = None,
    progress=None,
    opts: PeriodOptions | None = None,
    universe: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    cfg = cfg or Config()
    opts = opts or PeriodOptions()
    maps = build_maps(delivered, master, ref=ref, universe=universe)
    known_master_keys = {clean_name(n) for n in master["Granular Name"] if clean_name(n)}

    rows: list[dict] = []
    pending_idx: list[int] = []
    parent_hint: dict[int, ParentSignal] = {}
    pname_hint: dict[int, str] = {}
    capiq_hint: dict[int, str] = {}
    conflicts = 0
    capiq_ids_seen = 0
    n_rows = len(received)

    for i, r in enumerate(received.itertuples(index=False)):
        if progress and i % 500 == 0:
            progress(0.02 + 0.28 * i / max(n_rows, 1))
        parent_id = _s(getattr(r, "client_corigrpcomid", ""))
        parent_name = _s(getattr(r, "client_corigrpname", ""))
        key = clean_name(r.name)
        base = {
            "bank_row_id": r.bank_row_id, "client_leid": r.ciq_id, "client_lename": r.name,
            "client_corigrpcomid": parent_id, "client_corigrpname": parent_name,
            "Status": "Not Mapped", "Coalition ID": "", "Coalition Names": "",
            "tier": "", "evidence": "", "score": None,
            "inhouse_id": "", "inhouse_name": "", "matched_via": "", "relation": "",
            "clean_name": key, "bank_name": r.name, "bank_country": getattr(r, "country", ""),
            "status": "no_match", "decided": "", "parent_hint": "",
        }

        ps = _parent_signal(maps, parent_id, opts) if opts.use_parent_group else None
        if ps:
            parent_hint[i] = ps
            base["parent_hint"] = f"{ps.cid} ({ps.label()})"
        if opts.use_parent_name and parent_name:
            pc = maps.coalition_by_name.get(clean_name(parent_name))
            if pc:
                pname_hint[i] = pc
        if opts.use_capiq and r.ciq_id and maps.capiq:
            cc = maps.capiq.get(norm_capiq(r.ciq_id)) or maps.capiq.get(_s(r.ciq_id))
            if cc:
                capiq_hint[i] = cc
                capiq_ids_seen += 1

        hit = maps.by_id.get(r.ciq_id) if r.ciq_id else None
        if hit:
            rows.append(_fill_hit(base, hit, "delivered_id", "prior delivery (client_leid)", 1.0, "auto"))
            continue
        hit = maps.by_name.get(key) if key else None
        if hit:
            rows.append(_fill_hit(base, hit, "delivered_name", "prior delivery (name)", 1.0, "auto"))
            continue
        hit = maps.master_exact.get(key) if key else None
        if hit:
            rows.append(_fill_hit(base, hit, "master_exact", "exact name in master", 1.0, "auto"))
            continue
        if i in capiq_hint:
            cid = capiq_hint[i]
            rows.append(_fill_hit(base, (cid, maps.master_names.get(cid, "")), "capiq",
                                  "CapIQ id in universe", 1.0, "auto"))
            continue
        if ps and ps.kind == "unique":
            cid = ps.cid
            ev = f"{ps.label()} under '{parent_name or parent_id}'"
            if pname_hint.get(i) == cid:
                ev += "; parent name agrees"
            rows.append(_fill_hit(base, (cid, maps.master_names.get(cid, "")), "parent_unique", ev, 0.98, "auto"))
            continue
        if i in pname_hint and (ps is None or ps.kind == "thin"):
            cid = pname_hint[i]
            ev = f"parent '{parent_name}' is a Coalition name"
            if ps is not None and ps.cid == cid:
                ev += f"; {ps.label()} agree"
            rows.append(_fill_hit(base, (cid, maps.master_names.get(cid, "")), "parent_name", ev, 0.96, "auto"))
            continue
        if opts.use_master_contain and key:
            mc = _master_contain(maps, key)
            if mc:
                hit, via, n_shared = mc
                ev = f"contained in master '{via}'"
                strong = n_shared >= 3 or (ps is not None and ps.strong and ps.cid == hit[0])
                if strong:
                    rows.append(_fill_hit(base, hit, "master_contain", ev, 0.94, "auto",
                                          matched_via=f"master: {via}"))
                    continue
                row = dict(base)
                _set_review(row, hit, "master_contain", ev + " (weak)", 0.9)
                rows.append(row)
                pending_idx.append(i)
                continue
        if opts.use_master_fuzzy and key:
            mf = _master_fuzzy(maps, key, opts)
            if mf:
                hit, via, score, margin = mf
                if score >= opts.master_fuzzy_auto and margin >= opts.master_fuzzy_margin:
                    rows.append(_fill_hit(base, hit, "master_fuzzy", f"~'{via}' ({score:.0f})",
                                          round(score / 100, 4), "auto", matched_via=f"master: {via}"))
                    continue
                row = dict(base)
                _set_review(row, hit, "master_fuzzy", f"~'{via}' ({score:.0f}, gap {margin:.0f})",
                            round(score / 100, 4))
                rows.append(row)
                pending_idx.append(i)
                continue
        pending_idx.append(i)
        rows.append(base)

    if pending_idx and ref is not None:
        bank = received.iloc[pending_idx].reset_index(drop=True)
        bank = bank.reindex(columns=BANK_COLS + [c for c in bank.columns if c not in BANK_COLS], fill_value="")
        if progress:
            progress(0.35)

        def prog(p):
            if progress:
                progress(0.35 + 0.55 * p)

        matched = match(bank, ref, cfg, aliases={}, progress=prog, master_index={})
        by_rid = {str(r.bank_row_id): r for r in matched.itertuples(index=False)}
        for i in pending_idx:
            m = by_rid.get(str(received.iloc[i]["bank_row_id"]))
            if m is None:
                continue
            row = rows[i]
            had_review = row["status"] == "review"
            if m.status == "auto" and m.inhouse_id:
                cnames = maps.master_names.get(m.inhouse_id) or m.inhouse_name
                rows[i] = _fill_hit(
                    row, (m.inhouse_id, cnames), f"universe:{m.tier}", m.evidence or "universe match",
                    float(m.score) if m.score is not None else None, "auto",
                    matched_via=m.matched_via, relation=m.relation,
                )
            elif m.status == "review" and m.inhouse_id:
                cnames = maps.master_names.get(m.inhouse_id) or m.inhouse_name
                if had_review:
                    _add_candidate(row, m.inhouse_id, cnames,
                                   float(m.score) if m.score is not None else None)
                    row["evidence"] += f"; universe → {m.inhouse_id}"
                else:
                    _set_review(row, (m.inhouse_id, cnames), f"universe:{m.tier}",
                                m.evidence or "needs review",
                                float(m.score) if m.score is not None else None)
                    row["matched_via"], row["relation"] = m.matched_via, m.relation
                    for k in (2, 3):
                        iid = getattr(m, f"inhouse_id_{k}", "")
                        if iid:
                            _add_candidate(row, iid,
                                           maps.master_names.get(iid) or getattr(m, f"inhouse_name_{k}", ""),
                                           getattr(m, f"score_{k}", None))
            elif not had_review:
                row.update({
                    "tier": f"universe:{m.tier}" if m.tier else "universe",
                    "evidence": m.evidence or "no match",
                    "status": "no_match",
                })
    elif pending_idx:
        for i in pending_idx:
            if rows[i]["status"] != "review":
                rows[i].update({
                    "tier": "leftover",
                    "evidence": "no universe reference loaded",
                    "status": "review",
                })

    for i, row in enumerate(rows):
        ps, pn, cq = parent_hint.get(i), pname_hint.get(i), capiq_hint.get(i)
        cur = row.get("inhouse_id", "")
        if row["status"] == "auto" and opts.conflict_audit:
            disagree = []
            if ps and ps.cid != cur and row["tier"] not in ("delivered_id", "delivered_name"):
                _add_candidate(row, ps.cid, maps.master_names.get(ps.cid, ""), round(ps.share, 3))
                if ps.strong:
                    disagree.append(f"parent → {ps.cid} ({ps.label()})")
                else:
                    row["evidence"] += f"; note: parent {ps.label()} → {ps.cid}"
            if pn and pn != cur and row["tier"] not in ("delivered_id", "delivered_name", "parent_unique"):
                disagree.append(f"parent name → {pn}")
                _add_candidate(row, pn, maps.master_names.get(pn, ""), 0.9)
            if cq and cq != cur:
                disagree.append(f"CapIQ → {cq}")
                _add_candidate(row, cq, maps.master_names.get(cq, ""), 1.0)
            if disagree:
                row["status"], row["Status"] = "review", "Not Mapped"
                row["evidence"] = f"{row['evidence']}; conflict: " + "; ".join(disagree)
                conflicts += 1
            elif ps and ps.cid == cur and row["tier"].startswith(("universe", "master_")):
                row["evidence"] += "; parent agrees"
        elif row["status"] != "auto":
            if ps:
                cid = ps.cid
                if not cur:
                    _set_review(row, (cid, maps.master_names.get(cid, "")),
                                "parent_thin" if ps.kind == "thin" else "parent_majority",
                                f"{ps.label()} under '{row['client_corigrpname'] or row['client_corigrpcomid']}'",
                                round(ps.share, 3))
                elif cid != cur:
                    _add_candidate(row, cid, maps.master_names.get(cid, ""), round(ps.share, 3))
                    row["evidence"] += f"; parent → {cid} ({ps.label()})"
                else:
                    row["evidence"] += "; parent agrees"
            if pn:
                if not row.get("inhouse_id"):
                    _set_review(row, (pn, maps.master_names.get(pn, "")), "parent_name",
                                f"parent '{row['client_corigrpname']}' is a Coalition name", 0.9)
                elif pn != row.get("inhouse_id"):
                    _add_candidate(row, pn, maps.master_names.get(pn, ""), 0.9)

    if progress:
        progress(0.95)

    results = pd.DataFrame(rows)
    for c in ("inhouse_id_2", "inhouse_name_2", "score_2", "inhouse_id_3", "inhouse_name_3", "score_3"):
        if c not in results.columns:
            results[c] = "" if "name" in c or "id" in c else None
    updated_master, writebacks = apply_master_writebacks(master, results, known_master_keys)

    stats = {
        "received": len(results),
        "by_tier": results["tier"].value_counts().to_dict(),
        "by_status": results["status"].value_counts().to_dict(),
        "auto_mapped": int((results["status"] == "auto").sum()),
        "review": int((results["status"] == "review").sum()),
        "no_match": int((results["status"] == "no_match").sum()),
        "conflicts": conflicts,
        "capiq_available": bool(maps.capiq),
        "capiq_hits": capiq_ids_seen,
        "parent_groups": len(maps.parent_groups),
        "master_writebacks": writebacks,
        "master_rows": len(updated_master),
        "master_mapped": int((updated_master["Status"].str.lower() == "mapped").sum()),
    }
    if progress:
        progress(1.0)
    return results, updated_master, stats


def apply_master_writebacks(
    master: pd.DataFrame,
    results: pd.DataFrame,
    known_keys: set[str] | None = None,
) -> tuple[pd.DataFrame, int]:
    known_keys = known_keys if known_keys is not None else {clean_name(n) for n in master["Granular Name"]}
    stamp = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = master.copy()
    key_to_pos: dict[str, int] = {}
    for i, name in enumerate(updated["Granular Name"]):
        k = clean_name(name)
        if k and k not in key_to_pos:
            key_to_pos[k] = i

    writebacks = 0
    new_rows: list[dict] = []
    seen_new: set[str] = set()

    for r in results.to_dict("records"):
        if r.get("status") != "auto" or not r.get("Coalition ID"):
            continue
        cid = _s(r.get("Coalition ID"))
        cnames = _s(r.get("Coalition Names"))
        gname = _s(r.get("client_lename") or r.get("bank_name"))
        tier = _s(r.get("tier")) or "period"
        key = clean_name(gname)
        if not key or not cid:
            continue

        if key in key_to_pos:
            pos = key_to_pos[key]
            row = updated.iloc[pos]
            if _is_mapped(row["Status"], row["Coalition ID"]):
                continue
            idx = updated.index[pos]
            updated.at[idx, "Status"] = "Mapped"
            updated.at[idx, "Coalition ID"] = cid
            if cnames:
                updated.at[idx, "Coalition Names"] = cnames
            updated.at[idx, "Mapped At"] = stamp
            updated.at[idx, "Source"] = tier
            writebacks += 1
        elif key not in seen_new:
            seen_new.add(key)
            new_rows.append({
                "Granular Name": gname, "Status": "Mapped", "Coalition ID": cid,
                "Coalition Names": cnames, "Mapped At": stamp, "Source": tier,
            })
            writebacks += 1

    if new_rows:
        updated = pd.concat([updated, pd.DataFrame(new_rows)], ignore_index=True)
    for c in MASTER_OUT_COLS:
        if c not in updated.columns:
            updated[c] = ""
    return updated[MASTER_OUT_COLS], writebacks
