"""Build the universe master file: every entity related to your universe, with LEI and CIQ ID.

Run once, then monthly (or when the universe changes):

    python scripts/build_universe_master.py universe.csv \\
        --lei2 ~/Data/gleif/...-lei2-golden-copy.csv.zip \\
        --rr   ~/Data/gleif/...-rr-golden-copy.csv.zip \\
        --sp-ids                      # optional: fetch GLEIF's S&P CIQ <-> LEI mapping
        --out universe_master.xlsx

--sp-ids calls the GLEIF API with IDENTIFIERS ONLY (your LEIs and CIQ numbers; ~500 requests, ~10 min,
resumable). Without it the master still works; subsidiaries just have no CIQ IDs and LEIs can only be
checked by name.

Load the resulting .xlsx on the app's Reference screen. Matching then needs no GLEIF files and no network.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from entity_mapper.universe_build import build_universe_file  # noqa: E402

GUESS = {"inhouse_id": ["id"], "name": ["name"], "country": ["country"], "lei": ["lei"], "ciq_id": ["ciq", "capiq"]}


def read_any(path):
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(p, dtype=str)
    return pd.read_csv(p, dtype=str, sep=None, engine="python")


def guess(cols, field):
    for c in cols:
        low = f" {c.lower()} "
        if any(k in low for k in GUESS[field]):
            return c
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("universe")
    ap.add_argument("--lei2", required=True, help="GLEIF Level 1 golden copy (.csv or .csv.zip)")
    ap.add_argument("--rr", required=True, help="GLEIF Level 2 relationships (.csv or .csv.zip)")
    ap.add_argument("--sp-ids", action="store_true", help="fetch S&P CIQ IDs from the GLEIF API")
    ap.add_argument("--sp-cache", help="pre-fetched S&P mapping (pickle) to reuse")
    ap.add_argument("--out", default="universe_master.xlsx")
    for f in GUESS:
        ap.add_argument(f"--{f.replace('_', '-')}-col")
    a = ap.parse_args()

    raw = read_any(a.universe)
    cols = {f: getattr(a, f"{f}_col") or guess(raw.columns, f) for f in GUESS}
    print("Columns:", cols)
    inh = pd.DataFrame({f: raw[c] if c else "" for f, c in cols.items()}).fillna("")
    build_universe_file(inh, a.lei2, a.rr, a.out, sp_ids=a.sp_ids, sp_cache=a.sp_cache)


if __name__ == "__main__":
    main()
