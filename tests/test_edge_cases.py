"""Name-collision regression tests. Run: python -m pytest tests -q

Each case: (bank name, bank country, expected status, expected top in-house ID or None).
"""
import random

import pandas as pd
import pytest

from entity_mapper import Config, build_reference, match

INHOUSE = pd.DataFrame([
    ["X1", "Apple Inc", "US", "Cupertino", "HWUPKR0MPOU8FGXBT394"],
    ["X2", "Green Apple Ltd", "GB", "London", ""],
    ["X3", "Apple Hospitality REIT Inc", "US", "Richmond", ""],
    ["X4", "Apple Bank for Savings", "US", "New York", ""],
    ["X5", "Big Apple Capital LLC", "US", "New York", ""],
    ["X6", "Barclays Bank PLC", "GB", "London", ""],
    ["X7", "Barclays Capital Securities Ltd", "GB", "London", ""],
    ["X8", "Appen Ltd", "AU", "Sydney", ""],
    ["X9", "Northwind Fund 2 LP", "US", "Boston", ""],
] + [[f"F{i}", n, "GB", "London", ""] for i, n in enumerate(
    # realistic-size list so word statistics behave as they would on ~10k in-house names
    f"{''.join(random.Random(i).choices('bcdfghklmnprstvwz', k=3))}{random.Random(-i).choice(['ar', 'el', 'or', 'un'])} "
    f"{random.Random(i * 7).choice(['Capital', 'Bank', 'Trading', 'Partners', 'Securities', 'Holdings', 'Group', 'Fund'])} Ltd"
    for i in range(5000))],
    columns=["inhouse_id", "name", "country", "city", "lei"])
GLEIF = pd.DataFrame([{"lei": "HWUPKR0MPOU8FGXBT394", "parent_lei": "",
                       "names": ["Apple Inc.", "Apple Computer, Inc."], "country": "US", "city": "Cupertino"}])

CASES = [
    ("Apple Inc.", "US", "auto", "X1"),
    ("APPLE INC", "United States", "auto", "X1"),
    ("Apple Computer Inc", "US", "auto", "X1"),          # former name via GLEIF
    ("Apple", "US", "auto", "X1"),
    ("Apple", "", "review", "X1"),                        # one word, no country: never auto
    ("Green Apple Limited", "UK", "auto", "X2"),
    ("Green Apple", "GB", "auto", "X2"),
    ("Apple Hospitality", "US", "review", "X3"),
    ("Apple Bank", "US", "review", "X4"),                 # not Apple Inc
    ("Big Apple Capital", "US", "auto", "X5"),
    ("Apple Pie Bakery Ltd", "GB", "not_auto", None),     # starts with 'Apple': review as possible subsidiary
    ("Barclays", "GB", "review", "X6"),                   # ambiguous between two Barclays
    ("Barclays Bank", "GB", "auto", "X6"),
    ("Appen", "AU", "auto", "X8"),
    ("Northwind Fund 3 LP", "US", "not_auto", None),      # different fund number: never auto
]


@pytest.fixture(scope="module")
def results():
    ref = build_reference(INHOUSE, GLEIF)
    bank = pd.DataFrame([{"name": n, "country": c} for n, c, _, _ in CASES])
    return match(bank, ref, Config())


@pytest.mark.parametrize("i,case", list(enumerate(CASES)), ids=[c[0] for c in CASES])
def test_case(results, i, case):
    name, country, status, top = case
    row = results.iloc[i]
    if status == "not_auto":
        assert row["status"] != "auto", f"{name} auto-matched to {row['inhouse_name']}"
        return
    assert row["status"] == status, f"{name}: {row['status']} ({row['evidence']})"
    if top:
        assert row["inhouse_id"] == top, f"{name}: top was {row['inhouse_name']}"
    # Green Apple and Apple must never be auto-matched to each other
    if row["status"] == "auto":
        assert not ({"X1", "X2"} == {row["inhouse_id"], top} and row["inhouse_id"] != top)


# ---- Master mapping (previous human-verified mappings) ----
MASTER = pd.DataFrame([
    {"name": "APPLE COMPUTER CUPERTINO", "inhouse_id": "X1", "country": "US"},
    {"name": "Kinnor Pharma Sarl", "inhouse_id": "X4", "country": ""},        # deliberately odd target
    {"name": "Barclays", "inhouse_id": "X6", "country": "GB"},
    {"name": "Barclays", "inhouse_id": "X7", "country": "GB"},                # humans disagreed
    {"name": "Stale Old Co", "inhouse_id": "GONE1", "country": ""},           # ID no longer exists
])


@pytest.fixture(scope="module")
def master_results():
    from entity_mapper import build_master_index
    ref = build_reference(INHOUSE, GLEIF, master=MASTER)
    idx, stats = build_master_index(MASTER, ref.inhouse.index)
    assert stats["unknown_inhouse_id"] == 1
    bank = pd.DataFrame([
        {"name": "Apple Computer Cupertino", "country": "US"},   # exact past spelling
        {"name": "Apple Computer Cupertino", "country": "GB"},   # past mapping was US: not reused
        {"name": "Kinnor Pharma GmbH", "country": ""},           # legal form incompatible with Sarl
        {"name": "Barclays", "country": "GB"},                   # conflicting history -> review
        {"name": "Stale Old Co", "country": ""},
    ])
    return match(bank, ref, Config(), master_index=idx)


def test_master_exact_reuse(master_results):
    r = master_results.iloc[0]
    assert (r.status, r.tier, r.inhouse_id) == ("auto", "master", "X1")


def test_master_respects_country(master_results):
    assert master_results.iloc[1].tier != "master"


def test_master_respects_legal_form(master_results):
    assert master_results.iloc[2].tier != "master"


def test_master_conflict_goes_to_review(master_results):
    r = master_results.iloc[3]
    assert (r.status, r.tier) == ("review", "master")


def test_master_ignores_stale_ids(master_results):
    assert master_results.iloc[4].inhouse_id != "GONE1"


def test_default_country_applies():
    ref = build_reference(INHOUSE, GLEIF)
    bank = pd.DataFrame([{"name": "Apple", "country": ""}])
    assert match(bank, ref, Config()).iloc[0].status == "review"
    assert match(bank, ref, Config(default_country="US")).iloc[0].status == "auto"


# ---- Group-level universe: subsidiaries and same-name companies ----
GROUPS = pd.DataFrame([
    ["G1", "3M", "USA Midwest", "", ""], ["G2", "Merck & Co.", "USA New York", "", ""],
    ["G3", "Merck Kgaa", "Germany", "", ""], ["G4", "Glencore", "Switzerland", "", ""],
    ["G5", "Siemens", "Germany", "", ""], ["G6", "Siemens Energy Ag", "Germany", "", ""],
] + [[f"F{i}", n, "GB", "", ""] for i, n in enumerate(INHOUSE[INHOUSE.inhouse_id.str.startswith("F")].name)],
    columns=["inhouse_id", "name", "country", "lei", "ciq_id"])

GROUP_CASES = [
    ("3M United Kingdom PLC", "GB", "review", "G1"),          # subsidiary -> group, via review
    ("3M Company", "US", "auto", "G1"),
    ("Merck KGaA", "", "review", "G3"),                       # names only: legal form ranks the twin
    ("Merck & Co., Inc.", "US", "auto", "G2"),
    ("Glencore plc", "JE", "review", "G4"),                   # HQ CH vs incorporated JE
    ("Siemens Aktiengesellschaft", "DE", "auto", "G5"),
    ("Siemens Energy AG", "DE", "auto", "G6"),                # not Siemens
    ("Siemens Financial Services GmbH", "DE", "review", "G5"),
]


@pytest.mark.parametrize("case", GROUP_CASES, ids=[c[0] for c in GROUP_CASES])
def test_group_universe(case):
    name, country, status, top = case
    ref = build_reference(GROUPS)
    r = match(pd.DataFrame([{"name": name, "country": country}]), ref, Config()).iloc[0]
    assert (r.status, r.inhouse_id) == (status, top), f"{name}: {r.status} {r.inhouse_name} ({r.evidence})"


# ---- Lessons from the real GLEIF test (2026-09-27) ----
def test_brackets_are_not_alternative_names():
    from entity_mapper.normalize import split_variants, clean_name
    assert [clean_name(v) for v in split_variants("Hong Leong Company (Malaysia) Berhad")] == ["hong leong company malaysia"]
    assert [clean_name(v) for v in split_variants("Foo Ltd (formerly Bar Ltd)")] == ["foo", "bar"]


def test_country_in_brackets_does_not_match_other_companies():
    inh = pd.DataFrame([["H1", "Hong Leong Company (Malaysia) Berhad", "Malaysia", "", ""],
                        ["T1", "Tesco Plc", "United Kingdom", "", ""]] + GROUPS.values.tolist()[6:],
                       columns=["inhouse_id", "name", "country", "lei", "ciq_id"])
    r = match(pd.DataFrame([{"name": "Tesco Stores (Malaysia) Sdn Bhd", "country": "MY"}]),
              build_reference(inh), Config()).iloc[0]
    assert r.inhouse_id != "H1"


def test_gleif_subsidiaries():
    lei_g, lei_c1, lei_c2 = "5493001KJTIIGC8Y1R12", "5493000IBP32UQZ0KL24", "549300N9B2AWIRVCCJ88"
    inh = pd.DataFrame([["G1", "Bracco", "Italy", lei_g, ""], ["G2", "Johnson & Johnson", "USA South", "", ""]]
                       + GROUPS.values.tolist()[6:], columns=["inhouse_id", "name", "country", "lei", "ciq_id"])
    gleif = pd.DataFrame([
        {"lei": lei_g, "parent_lei": "", "names": ["BRACCO S.P.A."], "country": "IT", "city": ""},
        {"lei": lei_c1, "parent_lei": lei_g, "names": ["SPIN S.P.A."], "country": "IT", "city": ""},
        {"lei": lei_c2, "parent_lei": lei_g, "names": ["Bracco Imaging Deutschland GmbH"], "country": "DE", "city": ""},
    ])
    ref = build_reference(inh, gleif)
    bank = pd.DataFrame([{"name": "Bracco Imaging Deutschland GmbH", "country": "DE"},   # GLEIF child -> auto
                         {"name": "SPIN SRL", "country": "IT"},                          # thin name, other form
                         {"name": "SPIN S.p.A.", "country": "IT"},                       # thin, exact+country+form
                         {"name": "Unrelated", "country": "IT", "lei": lei_c1}])        # child LEI -> auto
    r = match(bank, ref, Config())
    assert (r.iloc[0].status, r.iloc[0].inhouse_id) == ("auto", "G1")
    assert r.iloc[1].status != "auto"
    assert (r.iloc[2].status, r.iloc[2].inhouse_id) == ("auto", "G1")
    assert (r.iloc[3].status, r.iloc[3].inhouse_id, r.iloc[3].tier) == ("auto", "G1", "id")


def test_universe_master_file_path():
    from entity_mapper.universe import MASTER_COLS, reference_inputs
    g_lei, s_lei = "5493001KJTIIGC8Y1R12", "5493000IBP32UQZ0KL24"
    rows = [
        {"coalition_id": "C1", "coalition_name": "Bracco", "role": "group", "entity_name": "BRACCO S.P.A.",
         "lei": g_lei, "lei_used": g_lei, "ciq_id": "IQ111", "country": "IT"},
        {"coalition_id": "C1", "coalition_name": "Bracco", "role": "subsidiary",
         "entity_name": "Bracco Imaging Deutschland GmbH", "lei": s_lei, "lei_used": s_lei, "ciq_id": "IQ222",
         "country": "DE"},
        {"coalition_id": "C2", "coalition_name": "Microlife Corporation", "role": "group",
         "entity_name": "Microlife Corporation", "lei": "IGJSJL3JD5P30I6NJZ34", "lei_used": "", "ciq_id": "IQ333",
         "country": "TW", "flag": "suspect LEI: not used"},
    ]
    um = pd.DataFrame(rows).reindex(columns=MASTER_COLS).fillna("")
    extra_inh = GROUPS.iloc[6:].rename(columns={"inhouse_id": "coalition_id", "name": "coalition_name"}).assign(
        role="group", entity_name=lambda d: d.coalition_name).reindex(columns=MASTER_COLS).fillna("")
    inh, subset, extra = reference_inputs(pd.concat([um, extra_inh]))
    ref = build_reference(inh, subset, None, extra_ciq=extra)
    bank = pd.DataFrame([{"name": "x", "ciq_id": "222"},                                   # subsidiary CIQ, no prefix
                         {"name": "Bracco Imaging Deutschland GmbH", "country": "DE"},     # subsidiary name
                         {"name": "y", "lei": "IGJSJL3JD5P30I6NJZ34"}])                    # suspect LEI: not used
    r = match(bank, ref, Config())
    assert (r.iloc[0].status, r.iloc[0].inhouse_id) == ("auto", "C1")
    assert (r.iloc[1].status, r.iloc[1].inhouse_id) == ("auto", "C1")
    assert r.iloc[2].inhouse_id != "C2"
