"""Generate synthetic in-house, bank and GLEIF files with known ground truth.

Output (sample_data/):
  inhouse.csv        ~10k entities: inhouse_id, name, country, city, lei, ciq_id
  bank_submission.csv up to 100k rows: bank_row_id, name, country, city, lei, true_inhouse_id
  gleif_subset.csv   simple-format GLEIF extract: lei, legal_name, other_names, country, city, parent_lei

The bank file mixes true matches (with realistic noise), GLEIF-only aliases,
subsidiaries of in-house parents, hard negatives and plain non-matches.
"""
import argparse
import random
import string
from pathlib import Path

import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from entity_mapper.normalize import clean_name  # noqa: E402

R = random.Random(42)

SYL = ["ar", "bel", "cor", "dan", "el", "fen", "gar", "hol", "ist", "jor", "kel", "lan", "mor",
       "nor", "ost", "pel", "quin", "ros", "sal", "tor", "ul", "var", "wes", "xan", "yor", "zen",
       "bri", "cal", "dor", "ev", "fal", "gre", "har", "ith", "kin", "lor", "mer", "nov", "or", "ren"]
PLACES = ["London", "Thames", "Albion", "Mercia", "Wessex", "Atlantic", "Northern", "Pacific",
          "Highland", "Crown", "Royal", "Union", "Metro", "Harbour", "Summit", "Meridian", "Oak",
          "Cedar", "Granite", "Silver", "Golden", "Blue", "Red", "Green", "Eagle", "Lion", "Phoenix"]
INDUSTRY = ["Capital", "Investments", "Asset Management", "Securities", "Bank", "Insurance",
            "Partners", "Advisors", "Trading", "Properties", "Energy", "Logistics", "Pharma",
            "Technologies", "Holdings", "Finance", "Fund", "Trust", "Ventures", "Industries"]
GENERIC = ["Group", "Holdings", "International", "Global", "", "", "", ""]
COUNTRIES = {"GB": (["Ltd", "Limited", "PLC", "LLP"], ["London", "Manchester", "Edinburgh"]),
             "US": (["Inc", "LLC", "Corp", "LP"], ["New York", "Boston", "Chicago"]),
             "DE": (["GmbH", "AG"], ["Frankfurt", "Munich", "Berlin"]),
             "FR": (["SA", "SAS"], ["Paris", "Lyon"]),
             "LU": (["SA", "Sarl", "SCSp"], ["Luxembourg"]),
             "IE": (["Limited", "DAC", "PLC"], ["Dublin"]),
             "NL": (["BV", "NV"], ["Amsterdam", "Rotterdam"]),
             "CH": (["AG", "SA"], ["Zurich", "Geneva"])}
CW = list(COUNTRIES)
CW_WEIGHTS = [40, 25, 8, 6, 8, 5, 4, 4]
COUNTRY_NAMES = {"GB": ["GB", "United Kingdom", "UK", "GBR"], "US": ["US", "USA", "United States"],
                 "DE": ["DE", "Germany"], "FR": ["FR", "France"], "LU": ["LU", "Luxembourg"],
                 "IE": ["IE", "Ireland"], "NL": ["NL", "Netherlands"], "CH": ["CH", "Switzerland"]}
ABBR = {"International": "Intl", "Management": "Mgmt", "Holdings": "Hldgs", "Group": "Grp",
        "Investments": "Invts", "Securities": "Secs", "Capital": "Cap", "Partners": "Ptnrs"}


def word():
    return "".join(R.choice(SYL) for _ in range(R.choice([2, 2, 3]))).capitalize()


def core():
    kind = R.random()
    if kind < 0.45:
        return word()
    if kind < 0.75:
        return f"{R.choice(PLACES)} {word()}"
    return f"{word()} {R.choice(PLACES)}"


def make_lei():
    base = "".join(R.choice(string.digits + string.ascii_uppercase) for _ in range(18))
    num = int("".join(str(int(c, 36)) for c in base + "00"))
    return base + f"{98 - num % 97:02d}"


def entity_name(country, c=None):
    c = c or core()
    parts = [c, R.choice(INDUSTRY)]
    g = R.choice(GENERIC)
    if g:
        parts.append(g)
    if "Fund" in parts and R.random() < 0.6:
        parts.append(str(R.randint(1, 9)))
    parts.append(R.choice(COUNTRIES[country][0]))
    return " ".join(parts)


def noisy(name, country):
    """Perturb a name the way bank ops teams do."""
    n = name
    forms = COUNTRIES[country][0]
    ops = R.sample(["form", "case", "abbr", "typo", "the", "amp", "drop", "punct", "none"],
                   k=R.choice([1, 2, 2, 3]))
    for op in ops:
        if op == "form":
            toks = n.split()
            if toks[-1] in sum((v[0] for v in COUNTRIES.values()), []):
                toks[-1] = R.choice(forms + [""])
            n = " ".join(t for t in toks if t)
        elif op == "case":
            n = R.choice([n.upper(), n.lower(), n.title()])
        elif op == "abbr":
            for k, v in ABBR.items():
                if k in n and R.random() < 0.7:
                    n = n.replace(k, v)
        elif op == "typo" and len(n) > 8:
            i = R.randrange(1, len(n) - 2)
            n = n[:i] + n[i + 1] + n[i] + n[i + 2:]  # transpose two characters
        elif op == "the":
            n = "The " + n
        elif op == "amp":
            n = n.replace(" and ", " & ")
        elif op == "drop":
            for g in ["Group ", "Holdings ", "International ", "Global "]:
                n = n.replace(g, "", 1)
        elif op == "punct":
            n = n.replace(" Ltd", " Ltd.").replace(" Inc", " Inc.").replace(" PLC", ", PLC")
    return n.strip()


def main(n_inhouse=10_000, n_bank=100_000, out="sample_data"):
    out = Path(out)
    out.mkdir(exist_ok=True)

    inh = []
    used = set()
    while len(inh) < n_inhouse:
        country = R.choices(CW, CW_WEIGHTS)[0]
        name = entity_name(country)
        if clean_name(name) in used:
            continue
        used.add(clean_name(name))
        i = len(inh) + 1
        inh.append({"inhouse_id": f"FC{i:06d}", "name": name, "country": country,
                    "city": R.choice(COUNTRIES[country][1]),
                    "lei": make_lei() if R.random() < 0.45 else "",
                    "ciq_id": f"IQ{R.randint(10**6, 10**8)}" if R.random() < 0.5 else ""})
    inh_df = pd.DataFrame(inh)

    # GLEIF extract: in-house LEIs (+ former names for 15%) and children of in-house parents
    gleif, former, children = [], {}, []
    for r in inh:
        if not r["lei"]:
            continue
        other = ""
        if R.random() < 0.15:
            other = entity_name(r["country"])
            if clean_name(other) in used:
                other = ""
            else:
                used.add(clean_name(other))
                former[r["inhouse_id"]] = other
        gleif.append({"lei": r["lei"], "legal_name": r["name"], "other_names": other,
                      "country": r["country"], "city": r["city"], "parent_lei": ""})
    parents = [r for r in inh if r["lei"]]
    for p in R.sample(parents, k=min(1500, len(parents))):
        c = R.choices(CW, CW_WEIGHTS)[0]
        base = p["name"].split()[0]
        child_name = f"{base} {R.choice(INDUSTRY)} {R.choice(COUNTRIES[c][0])}"
        child = {"lei": make_lei(), "legal_name": child_name, "other_names": "", "country": c,
                 "city": R.choice(COUNTRIES[c][1]), "parent_lei": p["lei"], "_parent": p["inhouse_id"]}
        children.append(child)
    gleif_df = pd.DataFrame(gleif + [{k: v for k, v in c.items() if k != "_parent"} for c in children])

    bank = []

    def add(name, country, truth, lei="", kind=""):
        bank.append({"name": name, "country": R.choice(COUNTRY_NAMES[country]) if country else "",
                     "city": "", "lei": lei, "true_inhouse_id": truth, "case_type": kind})

    n_true = int(n_bank * 0.08)
    for r in R.choices(inh, k=n_true):
        roll = R.random()
        if r["inhouse_id"] in former and roll < 0.5:
            add(noisy(former[r["inhouse_id"]], r["country"]), r["country"], r["inhouse_id"], kind="former_name")
        elif roll < 0.20 and r["lei"]:
            add(noisy(r["name"], r["country"]), r["country"], r["inhouse_id"], lei=r["lei"], kind="with_lei")
        else:
            country = r["country"] if R.random() < 0.85 else ""
            add(noisy(r["name"], r["country"]), country, r["inhouse_id"], kind="noisy_name")
    for c in R.sample(children, k=min(500, len(children))):
        add(noisy(c["legal_name"], c["country"]), c["country"], c["_parent"], kind="subsidiary")
    # hard negatives: same core, different business; or different fund number; or other country
    for r in R.choices(inh, k=int(n_bank * 0.03)):
        roll = R.random()
        toks = r["name"].split()
        if roll < 0.4:
            n = f"{toks[0]} {R.choice(INDUSTRY)} {R.choice(COUNTRIES[r['country']][0])}"
            if clean_name(n) in used:
                continue
            add(n, r["country"], "", kind="hard_neg_business")
        elif roll < 0.7 and any(t.isdigit() for t in toks):
            n = " ".join(str((int(t) % 9) + 1) if t.isdigit() else t for t in toks)
            add(n, r["country"], "", kind="hard_neg_number")
        else:
            c = R.choice([x for x in CW if x != r["country"]])
            n = " ".join(toks[:-1] + [R.choice(COUNTRIES[c][0])])
            add(n, c, "", kind="hard_neg_country")
    while len(bank) < n_bank:
        c = R.choices(CW, CW_WEIGHTS)[0]
        n = entity_name(c)
        if clean_name(n) in used:
            continue
        add(n, c, "", kind="non_match")
    R.shuffle(bank)
    bank_df = pd.DataFrame(bank)
    bank_df.insert(0, "bank_row_id", [f"B{i:06d}" for i in range(1, len(bank_df) + 1)])

    inh_df.to_csv(out / "inhouse.csv", index=False)
    bank_df.to_csv(out / "bank_submission.csv", index=False)
    gleif_df.to_csv(out / "gleif_subset.csv", index=False)
    print(f"in-house {len(inh_df):,} | bank {len(bank_df):,} | gleif {len(gleif_df):,}")
    print(bank_df["case_type"].value_counts().to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inhouse", type=int, default=10_000)
    ap.add_argument("--bank", type=int, default=100_000)
    ap.add_argument("--out", default="sample_data")
    a = ap.parse_args()
    main(a.inhouse, a.bank, a.out)
