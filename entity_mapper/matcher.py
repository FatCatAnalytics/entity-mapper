"""Tiered entity matcher: bank submission -> in-house list.

Tiers (first hit wins):
  alias     previous reviewer decision for this bank (from the workspace store)
  id        exact, checksum-valid LEI, or exact Capital IQ ID
  exact     identical cleaned name (any variant) with compatible country
  fuzzy     char 3-gram TF-IDF candidates, re-scored with several signals
GLEIF and CIQ are used as *bridges*: their names become extra match variants
for in-house entities, and GLEIF children map to in-house parents.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz.distance import OSA, JaroWinkler
from sklearn.feature_extraction.text import TfidfVectorizer

from .normalize import (INCORPORATION_HUBS, _LEGAL_FORMS, clean_id, form_country_ok, clean_name, digit_tokens, forms_compatible, legal_form,
                        lei_is_valid,
                        normalize_country, split_variants)

NO_MATCH = "__NO_MATCH__"

# Standard column names used inside the engine. The UI maps user columns to these.
INHOUSE_COLS = ["inhouse_id", "name", "country", "city", "lei", "ciq_id"]
BANK_COLS = ["bank_row_id", "name", "country", "city", "lei", "ciq_id"]


@dataclass
class Config:
    auto_score: float = 0.92      # best score needed for auto-match
    auto_margin: float = 0.06     # best minus runner-up needed for auto-match
    review_score: float = 0.80    # below this -> no match (calibrate on labelled data)
    top_k: int = 15               # TF-IDF candidates per query name
    rescore_n: int = 8            # distinct in-house entities re-scored per row
    show_n: int = 3               # candidates written to output
    auto_subsidiaries: bool = True   # GLEIF-confirmed subsidiary -> auto-match to the in-house group
    containment_review: bool = True  # "Apple Bank" inside "Apple Bank for Savings" -> always review
    default_country: str = ""        # applied to bank rows with no country (e.g. a domestic file)
    flag_possible_subsidiaries: bool = True  # "3M United Kingdom PLC" -> review against group "3M"
    subsidiary_score: float = 0.82
    chunk: int = 2000


@dataclass
class Reference:
    inhouse: pd.DataFrame
    variants: pd.DataFrame
    lei_map: dict = field(default_factory=dict)   # lei -> (inhouse_id, relation)
    ciq_map: dict = field(default_factory=dict)   # ciq_id -> inhouse_id
    exact: dict = field(default_factory=dict)     # clean -> list[variant idx]
    vectorizer: TfidfVectorizer | None = None
    matrix: object = None
    word_idf: dict = field(default_factory=dict)
    max_idf: float = 1.0
    rare_idf: float = 1.0          # tokens at/above this IDF are "distinctive"
    name_countries: dict = field(default_factory=dict)  # clean name -> countries (GLEIF + in-house)
    official_names: dict = field(default_factory=dict)  # clean -> in-house IDs (non-master variants)
    ciq_rel: dict = field(default_factory=dict)         # ciq -> "self" | "subsidiary"
    _common_vocab: list = field(default_factory=list)
    _fix_cache: dict = field(default_factory=dict)

    def canon(self, token: str) -> str:
        """Spell-correct an unseen token to a close *common* reference word
        ('capiatl' -> 'capital'). Distinctive words are never 'corrected'."""
        if token in self.word_idf or len(token) < 4:
            return token
        hit = self._fix_cache.get(token)
        if hit is None:
            m = process.extractOne(token, self._common_vocab,
                                   scorer=lambda a, b, **_: OSA.normalized_similarity(a, b) * 100,
                                   score_cutoff=75)
            hit = m[0] if m else token
            self._fix_cache[token] = hit
        return hit

    @property
    def stats(self) -> dict:
        v = self.variants
        return {
            "inhouse_entities": len(self.inhouse),
            "name_variants": len(v),
            "variants_by_source": v["source"].value_counts().to_dict(),
            "leis_indexed": len(self.lei_map),
            "ciq_ids_indexed": len(self.ciq_map),
        }


def _prep(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols].fillna("")
    for c in cols:
        df[c] = df[c].astype(str).str.strip()
    return df


def build_reference(inhouse: pd.DataFrame, gleif: pd.DataFrame | None = None,
                    ciq: pd.DataFrame | None = None,
                    name_index: dict | None = None,
                    master: pd.DataFrame | None = None,
                    extra_ciq: dict | None = None) -> Reference:
    inh = _prep(inhouse, INHOUSE_COLS)
    inh["inhouse_id"] = inh["inhouse_id"].map(clean_id)
    inh = inh[inh["inhouse_id"] != ""].drop_duplicates("inhouse_id")
    inh["country_iso"] = inh["country"].map(normalize_country)
    inh["lei"] = inh["lei"].map(clean_id)
    inh["ciq_id"] = inh["ciq_id"].map(clean_id)

    var_rows = []

    def add(iid, raw, source, relation, country, city):
        c = clean_name(raw)
        if c:
            city = city if isinstance(city, str) else ""
            var_rows.append((iid, raw, c, source, relation, country, city.lower().strip(),
                             legal_form(raw)))

    for r in inh.itertuples(index=False):
        for v in split_variants(r.name) or [r.name]:
            add(r.inhouse_id, v, "inhouse", "self", r.country_iso, r.city)

    lei_map = {r.lei: (r.inhouse_id, "self") for r in inh.itertuples() if lei_is_valid(r.lei)}
    ciq_map = {r.ciq_id: r.inhouse_id for r in inh.itertuples() if r.ciq_id}
    ciq_rel = {k: "self" for k in ciq_map}
    known_ids = set(inh["inhouse_id"])
    for extra_id, (iid, rel) in (extra_ciq or {}).items():   # subsidiaries' CIQ IDs from the universe master
        extra_id = clean_id(extra_id)
        if extra_id and extra_id not in ciq_map and iid in known_ids:
            ciq_map[extra_id], ciq_rel[extra_id] = iid, rel

    if gleif is not None and len(gleif):
        own_names: dict[str, set] = {}
        own_words: dict[str, set] = {}
        for row in var_rows:
            own_names.setdefault(row[2], set()).add(row[0])
            own_words.setdefault(row[0], set()).update(row[2].split())
        for g in gleif.itertuples(index=False):
            if g.parent_lei:
                target = lei_map.get(g.parent_lei)
                if not target or target[1] != "self":
                    continue
                iid, relation = target[0], "subsidiary"
                lei_map.setdefault(g.lei, (iid, relation))
            else:
                target = lei_map.get(g.lei)
                if not target:
                    continue
                iid, relation = target
            for n in g.names:
                # A subsidiary's name must never shadow an entity that is itself on the in-house list.
                if relation == "subsidiary":
                    # Never let a subsidiary name shadow a DIFFERENT entity on the in-house list.
                    # The group's own name is fine ("Johnson & Johnson SA", Spain -> J&J).
                    owners = own_names.get(clean_name(n))
                    if owners and iid not in owners:
                        continue
                add(iid, n, "gleif", relation, normalize_country(g.country), g.city or "")

    if master is not None and len(master):
        # Spellings banks used before for a known entity: 'ACME HLDGS UK' -> FC000123.
        # A past spelling that equals ANOTHER entity's official name is dropped: the
        # official name wins ('X Capital' recorded for X Capital Group, but X Capital exists).
        official: dict[str, set] = {}
        for row in var_rows:
            official.setdefault(row[2], set()).add(row[0])
        m = master.fillna("")
        known = set(inh["inhouse_id"])
        ctrys = m["country"] if "country" in m else [""] * len(m)
        for n, iid, ctry in zip(m["name"], m["inhouse_id"].map(clean_id), ctrys):
            if iid in known and iid != NO_MATCH:
                c = clean_name(n)
                if c in official and iid not in official[c]:
                    continue
                add(iid, n, "master", "self", normalize_country(ctry) or "", "")
        ref_official = official
    else:
        ref_official = None

    if ciq is not None and len(ciq):
        ciq = ciq.fillna("")
        for r in ciq.itertuples(index=False):
            iid = ciq_map.get(clean_id(getattr(r, "ciq_id", "")))
            if not iid:
                continue
            names = [getattr(r, "name", "")] + str(getattr(r, "aliases", "")).split(";")
            for n in names:
                if n and n.strip():
                    add(iid, n.strip(), "ciq", "self", normalize_country(getattr(r, "country", "")), "")

    variants = pd.DataFrame(var_rows, columns=["inhouse_id", "raw", "clean", "source",
                                               "relation", "country", "city", "form"])
    variants = variants.drop_duplicates(["inhouse_id", "clean", "relation"]).reset_index(drop=True)
    # "Thin" subsidiary names: one word that is not the group's own ("Delhome" for Ahold, "Spin"
    # for Bracco). Usable, but only an exact name + same country + same legal form auto-matches.
    group_words: dict[str, set] = {}
    for iid, c, rel in zip(variants["inhouse_id"], variants["clean"], variants["relation"]):
        if rel == "self":
            group_words.setdefault(iid, set()).update(c.split())
    variants["thin"] = [rel == "subsidiary" and len(c.split()) == 1 and c not in group_words.get(iid, set())
                        for iid, c, rel in zip(variants["inhouse_id"], variants["clean"], variants["relation"])]

    exact = defaultdict(list)
    for i, c in enumerate(variants["clean"]):
        exact[c].append(i)

    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 3), sublinear_tf=True,
                          dtype=np.float32)
    vec.fit(variants.loc[(variants["source"] != "master") & (variants["relation"] == "self"), "clean"])
    matrix = vec.transform(variants["clean"])

    # Word IDF: "capital", "holdings", "group" get low weight; "barclays" high.
    # Computed on official names only: historical bank spellings carry typos that must not
    # enter the vocabulary (they would look "distinctive" and block spell-correction).
    # ...and from the entities themselves, not their GLEIF subsidiaries (hundreds of "Siemens ..."
    # subsidiary names would otherwise make "siemens" look like a common word).
    official_clean = variants.loc[(variants["source"] != "master") & (variants["relation"] == "self"), "clean"]
    df_count: dict[str, int] = defaultdict(int)
    for c in official_clean:
        for t in set(c.split()):
            df_count[t] += 1
    n = max(len(official_clean), 1)
    word_idf = {t: float(np.log((1 + n) / (1 + d)) + 1) for t, d in df_count.items()}
    max_idf = float(np.log(1 + n) + 1)
    # A token is distinctive if it appears in fewer than 0.5% of names.
    rare_idf = float(np.log((1 + n) / (1 + max(2, 0.005 * n))) + 1)
    ref = Reference(inh.set_index("inhouse_id", drop=False), variants, lei_map, ciq_map,
                    dict(exact), vec, matrix, word_idf, max_idf, rare_idf)
    # A subsidiary name with no distinctive word ("Trading Limited", "UK Services") says nothing
    # about the group: keep its LEI link, but drop it as a name variant.
    sub = variants["relation"] == "subsidiary"
    generic = sub & ~variants["clean"].map(
        lambda c: any(word_idf.get(t, max_idf) >= rare_idf for t in c.split()))
    if generic.any():
        variants = variants[~generic].reset_index(drop=True)
        exact = defaultdict(list)
        for i, c in enumerate(variants["clean"]):
            exact[c].append(i)
        ref.variants, ref.exact, ref.matrix = variants, dict(exact), vec.transform(variants["clean"])
    ref._common_vocab = [t for t, w in word_idf.items() if w < rare_idf and len(t) >= 4]
    ref.official_names = ref_official or {}
    ref.ciq_rel = ciq_rel
    # Where is each name registered? In-house countries plus, if supplied, GLEIF-wide countries.
    nc: dict[str, set] = {}
    for c, ctry in zip(variants["clean"], variants["country"]):
        if ctry:
            nc.setdefault(c, set()).add(ctry)
    for c, ctrys in (name_index or {}).items():
        nc.setdefault(c, set()).update(x for x in ctrys if x)
    ref.name_countries = nc
    return ref


_FILLER = {"the", "teh", "and", "of", "for", "de", "la", "le", "und"}
_FORM_WORDS = {k.replace(".", "") for k in _LEGAL_FORMS} | {"co", "company", "limited"}


def _tsim(t: str, u: str) -> float:
    """Word similarity 0-100 that counts a transposition ('bnak'/'bank') as one edit."""
    return max(fuzz.ratio(t, u), OSA.normalized_similarity(t, u) * 100)


def _ignorable(t: str) -> bool:
    """Filler words and (possibly misspelt) legal forms: 'limietd', 'tld', 'mgbh'."""
    if t in _FILLER or t in _FORM_WORDS:
        return True
    return len(t) >= 3 and any(_tsim(t, f) >= 75 for f in _FORM_WORDS if len(f) >= 3)


def _token_overlap(a: list[str], b: list[str], idf: dict, max_idf: float) -> float:
    """IDF-weighted share of a's tokens found (fuzzily) in b."""
    if not a or not b:
        return 0.0
    num = den = 0.0
    for t in a:
        w = idf.get(t, max_idf)
        best = max(_tsim(t, u) for u in b) / 100
        num += w * (best if best >= 0.8 else 0.0)
        den += w
    return num / den if den else 0.0


def build_master_index(master: pd.DataFrame | None, known_ids) -> tuple[dict, dict]:
    """clean name -> {inhouse_id: {"n": times mapped, "countries": set}}; plus import stats."""
    idx: dict = {}
    stats = {"rows": 0, "used": 0, "unknown_inhouse_id": 0, "no_match_entries": 0}
    if master is None or not len(master):
        return idx, stats
    m = master.fillna("")
    known = set(known_ids)
    for n, iid, ctry in zip(m["name"], m["inhouse_id"].map(clean_id),
                            m["country"] if "country" in m else [""] * len(m)):
        stats["rows"] += 1
        c = clean_name(n)
        if not c:
            continue
        if iid in ("", NO_MATCH):
            stats["no_match_entries"] += 1
            continue
        if iid not in known:
            stats["unknown_inhouse_id"] += 1
            continue
        e = idx.setdefault(c, {}).setdefault(iid, {"n": 0, "countries": set(), "forms": set()})
        e["n"] += 1
        e["countries"].add(normalize_country(ctry))
        e["forms"].add(legal_form(n))
        stats["used"] += 1
    return idx, stats


def _score(bc, bcountry, bcity, bform, v, cos, ref: Reference, containment=True):
    """Combine name similarity with context. Returns (score, notes, auto_ok)."""
    bc = " ".join(ref.canon(t) for t in bc.split())
    vc = v["clean"] if v["source"] != "master" else " ".join(ref.canon(t) for t in v["clean"].split())
    at, bt = bc.split(), vc.split()
    # Weighted toward "are the bank's words in the candidate?" rather than the reverse:
    # in-house legal names are usually longer than what banks type.
    tok = (0.7 * _token_overlap(at, bt, ref.word_idf, ref.max_idf)
           + 0.3 * _token_overlap(bt, at, ref.word_idf, ref.max_idf))
    tsr = fuzz.token_sort_ratio(bc, vc) / 100
    jw = JaroWinkler.normalized_similarity(bc, vc)
    s = 0.35 * tok + 0.30 * cos + 0.15 * tsr + 0.20 * jw
    # Every word the bank wrote should appear in the candidate, even common ones:
    # "Apple Bank" belongs with "Apple Bank for Savings", not "Apple Inc".
    missing = [t for t in at if not _ignorable(t)
               and max((_tsim(t, u) for u in bt), default=0) < 75]
    if missing:
        s *= 0.92 ** len(missing)
    notes = [f"name={s:.2f}", f"tokens={tok:.2f}"]
    if missing:
        notes.append("bank words not in candidate: " + ",".join(missing[:3]))
    elif containment and any(ref.word_idf.get(t, ref.max_idf) >= ref.rare_idf for t in at):
        # Containment: every bank word, including a distinctive one, is in the candidate
        # ("Apple Bank" in "Apple Bank for Savings"). Always worth a reviewer's look.
        if s < 0.85:
            s = 0.85
            notes.append("all bank words contained in candidate")
    auto_ok = True
    # Distinctive tokens must match exactly for an auto-match.
    rare_a = {t for t in at if ref.word_idf.get(t, ref.max_idf) >= ref.rare_idf}
    rare_b = {t for t in bt if ref.word_idf.get(t, ref.max_idf) >= ref.rare_idf}
    if rare_a != rare_b:
        auto_ok = False
        missing = sorted(rare_a ^ rare_b)[:3]
        notes.append("distinctive words differ: " + ",".join(missing))
    if digit_tokens(bc) != digit_tokens(vc):
        s *= 0.80
        auto_ok = False
        notes.append("numbers differ")
    if not bcountry and len(ref.name_countries.get(vc, ())) > 1:
        auto_ok = False
        notes.append(f"name registered in {len(ref.name_countries[vc])} countries; no country given")
    if not bcountry and form_country_ok(bform, v["country"]) is False:
        # No country supplied, but the legal form rules out the candidate's country
        # ("Foo GmbH" cannot be the UK "Foo Ltd").
        s *= 0.85
        auto_ok = False
        notes.append(f"legal form {bform} not used in {v['country']}")
    if bcountry and v["country"]:
        if bcountry != v["country"]:
            hub = bcountry in INCORPORATION_HUBS or v["country"] in INCORPORATION_HUBS
            s *= 0.92 if hub else 0.75   # HQ vs country of incorporation (Glencore: CH vs JE)
            auto_ok = False
            notes.append(f"country {bcountry}!={v['country']}" + (" (incorporation hub?)" if hub else ""))
        else:
            notes.append("country ok")
    else:
        s *= 0.98
    if not forms_compatible(bform, v["form"]):
        s *= 0.95
        auto_ok = False
        notes.append(f"legal form {bform}!={v['form']}")
    if bcity and v["city"] and bcity == v["city"]:
        s = min(1.0, s + 0.02)
        notes.append("city ok")
    if v["relation"] == "subsidiary":
        s *= 0.95
        notes.append("via GLEIF child")
        if v.get("thin"):
            auto_ok = False
    return float(s), notes, auto_ok


def _add_group_prefix(cands: list, variants: list[str], ref: Reference, vrec: list, cfg: Config):
    """If the bank name starts with an in-house name ('3m united kingdom' starts with '3m'),
    that entity is a possible parent: add it as a review candidate, never auto.
    The in-house name must be distinctive, so 'bank ...' or 'global ...' never trigger it."""
    by_id = {c["inhouse_id"]: c for c in cands}
    for v in variants:
        toks = v.split()
        for k in range(len(toks) - 1, 0, -1):
            prefix = " ".join(toks[:k])
            hits = [vrec[j] for j in ref.exact.get(prefix, []) if vrec[j]["source"] != "master"]
            if not hits:
                continue
            if not any(ref.word_idf.get(t, ref.max_idf) >= ref.rare_idf for t in toks[:k]):
                break  # generic prefix, ignore
            for vr in hits:
                note = f"bank name starts with '{prefix}': possible subsidiary or division"
                cur = by_id.get(vr["inhouse_id"])
                if cur is None:
                    new = {**vr, "score": cfg.subsidiary_score, "notes": [note], "auto_ok": False,
                           "relation": "possible subsidiary"}
                    cands.append(new)
                    by_id[vr["inhouse_id"]] = new
                elif cur["score"] < cfg.subsidiary_score:
                    cur.update(score=cfg.subsidiary_score, auto_ok=False, relation="possible subsidiary",
                               notes=cur["notes"] + [note])
            return  # longest prefix only


def match(bank: pd.DataFrame, ref: Reference, cfg: Config | None = None,
          aliases: dict | None = None, progress=None,
          master_index: dict | None = None) -> pd.DataFrame:
    """Return one output row per bank row."""
    cfg = cfg or Config()
    aliases = aliases or {}
    master_index = master_index or {}
    b = _prep(bank, BANK_COLS).reset_index(drop=True)
    if (b["bank_row_id"] == "").all():
        b["bank_row_id"] = (b.index + 1).astype(str)
    b["country_iso"] = b["country"].map(normalize_country)
    if cfg.default_country:
        b.loc[b["country_iso"] == "", "country_iso"] = normalize_country(cfg.default_country)
    b["city_l"] = b["city"].str.lower().str.strip()
    b["lei_c"] = b["lei"].map(clean_id)
    b["ciq_c"] = b["ciq_id"].map(clean_id)
    b["variants"] = b["name"].map(lambda n: [clean_name(v) for v in (split_variants(n) or [n])])
    b["variants"] = b["variants"].map(lambda vs: [v for v in dict.fromkeys(vs) if v])
    b["clean"] = b["variants"].map(lambda vs: vs[0] if vs else "")
    b["form"] = b["name"].map(legal_form)

    V = ref.variants
    vrec = V.to_dict("records")
    inh_name = ref.inhouse["name"].to_dict()
    out: list[dict | None] = [None] * len(b)

    def result(i, status, tier, cands, evidence):
        r = b.loc[i]
        row = {"bank_row_id": r.bank_row_id, "bank_name": r["name"], "bank_country": r.country,
               "clean_name": r.clean, "status": status, "tier": tier, "evidence": evidence}
        for k in range(cfg.show_n):
            c = cands[k] if k < len(cands) else None
            sfx = "" if k == 0 else f"_{k + 1}"
            row[f"inhouse_id{sfx}"] = c["inhouse_id"] if c else ""
            row[f"inhouse_name{sfx}"] = inh_name.get(c["inhouse_id"], "") if c else ""
            row[f"score{sfx}"] = round(c["score"], 4) if c else None
            if k == 0:
                row["matched_via"] = f'{c["source"]}: {c["raw"]}' if c else ""
                row["relation"] = c["relation"] if c else ""
        s1 = cands[0]["score"] if cands else 0.0
        s2 = cands[1]["score"] if len(cands) > 1 else 0.0
        row["margin"] = round(s1 - s2, 4) if cands else None
        out[i] = row

    todo = []
    for i, r in enumerate(b.itertuples(index=False)):
        # Tier: alias memory
        key = (r.clean, r.country_iso)
        hit = aliases.get(key) or aliases.get((r.clean, ""))
        if hit:
            if hit == NO_MATCH:
                result(i, "no_match", "alias", [], "reviewer previously marked no match")
            else:
                result(i, "auto", "alias", [{"inhouse_id": hit, "score": 1.0, "source": "alias",
                                             "raw": r.name, "relation": "self"}],
                       "reviewer previously confirmed")
            continue
        # Tier: master mapping (previous human-verified mappings, any bank)
        prev = master_index.get(r.clean)
        if prev:
            # Same checks as any other exact hit: country must agree when both are known, and the
            # legal form must be compatible ("Kinnor Pharma GmbH" is not "Kinnor Pharma Sarl").
            hits = {}
            clash = ref.official_names.get(r.clean, set())
            for iid, e in prev.items():
                if clash and iid not in clash:
                    continue  # the official name of another entity wins
                if r.country_iso and not (r.country_iso in e["countries"] or "" in e["countries"]):
                    continue
                forms = {f for f in e["forms"] | {legal_form(ref.inhouse.at[iid, "name"])} if f}
                if r.form and forms and not any(forms_compatible(r.form, f) for f in forms):
                    continue
                if not r.country_iso and form_country_ok(r.form, ref.inhouse.at[iid, "country_iso"]) is False:
                    continue
                hits[iid] = e
            if hits:
                cands = [{"inhouse_id": iid, "score": 1.0, "source": "master", "raw": r.name,
                          "relation": "self", "n": e["n"]}
                         for iid, e in sorted(hits.items(), key=lambda kv: -kv[1]["n"])]
                ev = f"previous mapping ({cands[0]['n']}x)"
                status = "auto"
                if len(cands) > 1:
                    status = "review"
                    ev += f"; previous mappings disagree ({len(cands)} in-house IDs)"
                    for c in cands[1:]:
                        c["score"] = round(c["n"] / (cands[0]["n"] + c["n"]), 3)
                lei_hit = ref.lei_map.get(r.lei_c) if r.lei_c and lei_is_valid(r.lei_c) else None
                if lei_hit and lei_hit[0] != cands[0]["inhouse_id"]:
                    status = "review"
                    ev += f"; bank LEI points to {lei_hit[0]}"
                if not r.country_iso and len(ref.name_countries.get(r.clean, ())) > 1:
                    status = "review"
                    ev += "; name registered in several countries, no country given"
                result(i, status, "master", cands, ev)
                continue
        # Tier: identifiers
        if r.lei_c:
            if lei_is_valid(r.lei_c) and r.lei_c in ref.lei_map:
                iid, rel = ref.lei_map[r.lei_c]
                status = "auto" if rel == "self" or cfg.auto_subsidiaries else "review"
                result(i, status, "id", [{"inhouse_id": iid, "score": 1.0, "source": "lei",
                                          "raw": r.lei_c, "relation": rel}],
                       "LEI match" + (" (GLEIF child of in-house parent)" if rel != "self" else ""))
                continue
        ciq_key = r.ciq_c if not r.ciq_c or r.ciq_c.startswith("IQ") else f"IQ{r.ciq_c}"
        if ciq_key and ciq_key in ref.ciq_map:
            rel = ref.ciq_rel.get(ciq_key, "self")
            result(i, "auto" if rel == "self" or cfg.auto_subsidiaries else "review", "id",
                   [{"inhouse_id": ref.ciq_map[ciq_key], "score": 1.0, "source": "ciq_id", "raw": ciq_key,
                     "relation": rel}],
                   "Capital IQ ID match" + (" (subsidiary of in-house group)" if rel != "self" else ""))
            continue
        # Tier: exact cleaned name
        cand = {}
        for v in r.variants:
            for j in ref.exact.get(v, []):
                vr = vrec[j]
                if r.country_iso and vr["country"] and r.country_iso != vr["country"]:
                    if not (r.country_iso in INCORPORATION_HUBS or vr["country"] in INCORPORATION_HUBS):
                        continue
                    vr = {**vr, "_hub": True}   # same name, HQ vs incorporation country: review
                cand.setdefault(vr["inhouse_id"], vr)
        if cand:
            cands = []
            for iid, vr in cand.items():
                sc = 1.0 if (r.country_iso and vr["country"]) else 0.97
                if vr.get("_hub"):
                    sc = 0.9
                # Same name twice (Merck KGaA vs Merck & Co.): the matching legal form ranks first.
                if r.form and len(cand) > 1 and legal_form(ref.inhouse.at[iid, "name"]) == r.form:
                    sc = min(1.0, sc + 0.01)
                if vr["relation"] == "subsidiary":
                    sc *= 0.95
                cands.append({**vr, "score": sc})
            cands.sort(key=lambda c: -c["score"])
            only = len(cands) == 1
            ok_rel = cands[0]["relation"] == "self" or cfg.auto_subsidiaries
            form_ok = forms_compatible(r.form, cands[0]["form"])
            # A one-word name ("Apple") needs agreeing countries to auto-match.
            short_ok = (len(r.clean.split()) > 1 or bool(r.country_iso and cands[0]["country"])
                        or form_country_ok(r.form, cands[0]["country"]) is True)
            short_ok = short_ok and not cands[0].get("_hub")
            n_ctry = len(ref.name_countries.get(cands[0]["clean"], ()))
            geo_ok = bool(r.country_iso) or (form_country_ok(r.form, cands[0]["country"]) is not False
                                             and n_ctry <= 1)
            thin_ok = (not cands[0].get("thin")) or (
                r.country_iso and r.country_iso == cands[0]["country"]
                and r.form and r.form == cands[0].get("form"))
            status = ("auto" if only and ok_rel and form_ok and short_ok and geo_ok and thin_ok
                      and cands[0]["score"] >= cfg.auto_score else "review")
            ev = "exact cleaned name" + ("" if only else f"; {len(cands)} in-house entities share it")
            if not thin_ok:
                ev += "; one-word subsidiary name: needs same country and legal form to auto-match"
            if cands[0].get("_hub"):
                ev += (f"; country {r.country_iso} vs in-house {cands[0]['country']}"
                       " (incorporation vs HQ?)")
            elif not short_ok:
                ev += "; one-word name without country"
            if not geo_ok:
                ev += (f"; name registered in {n_ctry} countries, no country given" if n_ctry > 1
                       else f"; legal form {r.form} not used in {cands[0]['country']}")
            if not form_ok:
                ev += f"; legal form {r.form}!={cands[0]['form']}"
            result(i, status, "exact", cands, ev)
            continue
        if r.clean:
            todo.append(i)
        else:
            result(i, "no_match", "none", [], "empty name")

    # Tier: fuzzy. Stack every name variant of every remaining row as a query.
    q_owner, q_text = [], []
    for i in todo:
        for v in b.at[i, "variants"]:
            q_owner.append(i)
            q_text.append(v)
    best_cos: dict[int, dict[int, float]] = defaultdict(dict)  # row -> {variant_idx: cos}
    q_best_variant: dict[tuple[int, int], str] = {}
    if q_text:
        Q = ref.vectorizer.transform(q_text)
        MT = ref.matrix.T.tocsr()
        k = min(cfg.top_k, ref.matrix.shape[0])
        for start in range(0, Q.shape[0], cfg.chunk):
            S = (Q[start:start + cfg.chunk] @ MT).toarray()
            idx = np.argpartition(-S, k - 1, axis=1)[:, :k]
            for qi in range(S.shape[0]):
                owner = q_owner[start + qi]
                d = best_cos[owner]
                for j in idx[qi]:
                    c = float(S[qi, j])
                    if c > d.get(j, -1):
                        d[j] = c
                        q_best_variant[(owner, j)] = q_text[start + qi]
            if progress:
                progress(min(1.0, (start + cfg.chunk) / Q.shape[0]))

    for i in todo:
        r = b.loc[i]
        per_entity: dict[str, tuple[float, int]] = {}
        for j, c in best_cos.get(i, {}).items():
            iid = vrec[j]["inhouse_id"]
            if c > per_entity.get(iid, (-1, -1))[0]:
                per_entity[iid] = (c, j)
        top = sorted(per_entity.items(), key=lambda kv: -kv[1][0])[:cfg.rescore_n]
        cands = []
        for iid, (c, j) in top:
            vr = vrec[j]
            s, notes, auto_ok = _score(q_best_variant[(i, j)], r.country_iso, r.city_l, r.form,
                                       vr, c, ref, cfg.containment_review)
            cands.append({**vr, "score": s, "notes": notes, "auto_ok": auto_ok})
        if cfg.flag_possible_subsidiaries:
            _add_group_prefix(cands, b.at[i, "variants"], ref, vrec, cfg)
        cands.sort(key=lambda c: -c["score"])
        if not cands or cands[0]["score"] < cfg.review_score:
            result(i, "no_match", "fuzzy", cands, "best score below review threshold")
            continue
        s1 = cands[0]["score"]
        s2 = cands[1]["score"] if len(cands) > 1 else 0.0
        ok_rel = cands[0]["relation"] == "self" or cfg.auto_subsidiaries
        if s1 >= cfg.auto_score and (s1 - s2) >= cfg.auto_margin and ok_rel and cands[0]["auto_ok"]:
            status = "auto"
        else:
            status = "review"
        ev = "; ".join(cands[0]["notes"])
        if s1 - s2 < cfg.auto_margin and len(cands) > 1:
            ev += f"; close runner-up ({s2:.2f})"
        result(i, status, "fuzzy", cands, ev)

    return pd.DataFrame(out)


def summarize(results: pd.DataFrame) -> dict:
    by_status = results["status"].value_counts().to_dict()
    by_tier = results.groupby(["tier", "status"]).size().unstack(fill_value=0)
    return {"by_status": by_status, "by_tier": by_tier}
