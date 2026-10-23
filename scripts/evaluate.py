"""Run the matcher on files with a ground-truth column and report precision/recall.

    python scripts/evaluate.py sample_data/inhouse.csv sample_data/bank_submission.csv \
        --gleif sample_data/gleif_subset.csv --truth true_inhouse_id
"""
import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from entity_mapper import (Config, build_master_index, build_reference, load_gleif_subset,  # noqa: E402
                           match)
from entity_mapper.gleif import name_country_index  # noqa: E402
from entity_mapper.normalize import clean_name  # noqa: E402


def evaluate(res: pd.DataFrame, truth: pd.Series, kinds: pd.Series | None = None) -> None:
    t = truth.fillna("").astype(str).values
    pred = res["inhouse_id"].fillna("").values
    st = res["status"].values
    df = pd.DataFrame({"truth": t, "pred": pred, "status": st})
    df["has_truth"] = df.truth != ""
    df["correct"] = df.pred == df.truth
    if kinds is not None:
        df["kind"] = kinds.values

    print("\nOutcome by status")
    for s in ["auto", "review", "no_match"]:
        d = df[df.status == s]
        if s == "no_match":
            missed = int(d.has_truth.sum())
            print(f"  {s:9s} {len(d):>7,} rows | true matches missed here: {missed:,}")
        else:
            prec = (d.correct & d.has_truth).mean() if len(d) else float("nan")
            print(f"  {s:9s} {len(d):>7,} rows | top candidate correct: {prec:.1%}")
    auto = df[df.status == "auto"]
    fp = int((~auto.correct).sum())
    total_true = int(df.has_truth.sum())
    auto_tp = int((auto.correct & auto.has_truth).sum())
    rev = df[df.status == "review"]
    rev_tp = int((rev.correct & rev.has_truth).sum())
    print(f"\nAuto precision {auto_tp / max(len(auto), 1):.2%} ({fp:,} wrong auto-matches)")
    print(f"Recall, auto only       {auto_tp / max(total_true, 1):.1%}")
    print(f"Recall, auto + review   {(auto_tp + rev_tp) / max(total_true, 1):.1%}  "
          f"(review queue: {len(rev):,} rows)")
    if kinds is not None:
        print("\nBy case type (share with each status; 'right' = top candidate is the truth)")
        g = df.groupby("kind")
        tab = pd.DataFrame({
            "rows": g.size(),
            "auto": g.apply(lambda x: (x.status == "auto").mean()),
            "review": g.apply(lambda x: (x.status == "review").mean()),
            "no_match": g.apply(lambda x: (x.status == "no_match").mean()),
            "auto_right": g.apply(lambda x: (x[x.status == "auto"].correct).mean()),
        })
        print(tab.to_string(float_format=lambda v: f"{v:.1%}"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inhouse")
    ap.add_argument("bank")
    ap.add_argument("--gleif")
    ap.add_argument("--truth", default="true_inhouse_id")
    ap.add_argument("--out")
    ap.add_argument("--master", help="previous mappings CSV: bank_name, inhouse_id, country")
    ap.add_argument("--name-index", action="store_true",
                    help="use GLEIF-wide name/country index (names-only safety net)")
    a = ap.parse_args()

    inh = pd.read_csv(a.inhouse, dtype=str)
    bank = pd.read_csv(a.bank, dtype=str)
    t0 = time.time()
    gleif = load_gleif_subset(a.gleif, set(inh["lei"].dropna())) if a.gleif else None
    index = None
    if a.gleif and a.name_index:
        index = name_country_index(a.gleif, {clean_name(n) for n in inh["name"].dropna()})
    master = None
    if a.master:
        master = pd.read_csv(a.master, dtype=str).rename(columns={"bank_name": "name"})
    ref = build_reference(inh, gleif, name_index=index, master=master)
    midx, mstats = build_master_index(master, ref.inhouse.index)
    if master is not None:
        print(f"Master mapping: {mstats}")
    t1 = time.time()
    print(f"Reference built in {t1 - t0:.1f}s: {ref.stats}")
    res = match(bank, ref, Config(), master_index=midx)
    t2 = time.time()
    print(f"Matched {len(bank):,} rows in {t2 - t1:.1f}s")
    print(res.groupby(["tier", "status"]).size().unstack(fill_value=0).to_string())
    evaluate(res, bank[a.truth], bank.get("case_type"))
    if a.out:
        res.to_csv(a.out, index=False)


if __name__ == "__main__":
    main()
