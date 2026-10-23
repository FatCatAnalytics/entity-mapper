# Entity Mapper (v1)

Maps a bank's client submission onto the in-house entity list, entirely on this machine.
Windows desktop app (Tauri window around the Python engine); the same UI runs in a browser on macOS.

## How it fits together

```
Tauri window (src-tauri)  --starts-->  engine (Python: FastAPI + matching)  --serves-->  React UI (web/dist)
                                        |
                                        +-- local workspace  ~/EntityMapper   (DuckDB, uploads, copy of the reference)
                                        +-- shared folder    \\server\team\EntityMapper   (plain files, see below)
```

The window is only a frame: it starts the engine on a free 127.0.0.1 port, waits for it, shows its pages and
stops it on close. The UI uses nothing Tauri-specific, so `python run.py` in a browser is the same app.

## On start

The Start tab checks, in order: engine running, shared folder reachable, published reference (copied and
loaded if newer than the local copy), master mapping and everyone's decisions merged. If the shared folder is
offline the last local copy is used and decisions are sent on the next start.

## Shared folder

Set once per user on the Start tab (or the `ENTITY_MAPPER_SHARED` environment variable). Plain files only:

```
config.json                      {"owner": "<username>"}  (whoever publishes first; edit to change)
reference/manifest.json          which universe master is current
reference/universe_master_*.xlsx
master/master_mapping.csv        previous mappings (Reference data tab > Import mapping)
decisions/<username>.jsonl       append-only, one file per user
```

No database file is opened on the network drive: each user appends only to their own log, and the app merges
all logs on start. Per bank + cleaned name + country: the owner's decision wins; otherwise the latest wins and
disagreements between users are listed on the Alias memory tab for the owner to settle.
The logs contain client names, so the folder must be an internal drive.

## Owner: monthly reference build (Owner tab)

1. Load the universe list and map its columns.
2. GLEIF Golden Copy: download in the app (public data, ~530 MB, resumable; kept in `~/EntityMapper/gleif`),
   or point to the two files if the proxy blocks the download.
3. Optional: S&P Capital IQ IDs from the GLEIF API (identifiers only, ~10 minutes).
4. Build, download the file, review the "LEI decisions" sheet, then Publish. Everyone gets it on next start.

`scripts/build_universe_master.py` still does the same build from the command line.

## Build the Windows installer

Needs Python 3.10+, Node 18+, Rust (stable, MSVC toolchain) and the WebView2 runtime (part of Windows 11).

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements-build.txt
npm run setup            # installs the Tauri CLI and the web packages
npm run build            # web UI -> engine (PyInstaller) -> NSIS installer
```

The installer is `src-tauri\target\release\bundle\nsis\Entity Mapper_1.0.0_x64-setup.exe` (per-user install,
no admin rights). `npm run dev` opens the window against the engine run from source.
Engine log: `%USERPROFILE%\EntityMapper\logs\engine.log`.

## Run it in a browser (macOS, or Windows without the shell)

Python 3.10+ and Node 18+.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/make_sample_data.py                      # synthetic test data with ground truth
python run.py                                           # opens http://127.0.0.1:8765
```

`web/dist` ships prebuilt, so Node is only needed to change the UI:

```bash
cd web && npm install
npm run dev        # hot-reload UI on http://127.0.0.1:5173, proxies /api to run.py on :8765
npm run build      # rebuild web/dist, which run.py serves
```

Architecture: React (Vite + TypeScript) front end, FastAPI back end wrapping the Python matching
engine. Both bind to 127.0.0.1 only. API docs at http://127.0.0.1:8765/api/docs.
The workspace (alias memory, run history, audit log, uploads) lives in `~/EntityMapper/`;
set `ENTITY_MAPPER_HOME` to move it.

## Workflow

1. **Start**: the checks above. Without a shared folder, load a universe master file (or a plain in-house
   list with optional GLEIF files) by hand on the **Reference data** tab.
2. **Match**: enter a bank code, drop the bank file (or paste rows from a spreadsheet for a small list), map
   columns, read the checks (blank names, IDs damaged by a spreadsheet, bad LEIs, repeats), run.
3. **Review**: work the review queue (accept, pick candidate 2/3, enter an ID, or mark no match),
   then save. Decisions are remembered per bank and reused on that bank's next file.
4. Download results as Excel (auto / review / no match sheets).

## Matching tiers (first hit wins)

| Tier | Rule | Outcome |
| --- | --- | --- |
| alias | same bank + cleaned name + country previously decided | auto / no match |
| id | checksum-valid LEI or Capital IQ ID present in the in-house list | auto (GLEIF child: review) |
| exact | identical cleaned name (any variant) and compatible country | auto if unique and legal form compatible |
| fuzzy | char 3-gram TF-IDF top-15, re-scored | auto / review / no match by score and margin |

Fuzzy score = 0.35 IDF-weighted token overlap + 0.30 TF-IDF cosine + 0.15 token-sort ratio
+ 0.20 Jaro-Winkler, then penalties for country, legal form, number and parent-link mismatches.
Auto-match also requires: distinctive words identical, same numbers, same country and a compatible
legal form, with a clear gap to the runner-up.

## GLEIF Golden Copy

Download two files from https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy
(or the API: https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest):

- Level 1 LEI-CDF, CSV (`...-lei2-golden-copy.csv.zip`, ~480 MB, 3.4M entities)
- Level 2 relationships, CSV (`...-rr-golden-copy.csv.zip`, ~23 MB)

Point the Reference screen at the .zip files as downloaded; nothing is unpacked. One pass (~80 s on the
test machine) keeps only what matters and is cached, so later builds are instant:

- GLEIF legal, other and transliterated names for your LEIs ("Bayerische Motoren Werke AG" -> BMW)
- subsidiaries of your groups via consolidation and branch links only; fund-management links are ignored
- successor LEIs of retired/merged LEIs
- a name -> countries index used as a safety net for name-only bank files

Subsidiary rules: GLEIF-confirmed subsidiaries auto-match to the group (setting on the match screen).
A subsidiary name never shadows a different entity on your list; one-word subsidiary names ("Delhome")
auto-match only on exact name + same country + same legal form; subsidiary names made only of common
words are ignored. A bank name that merely starts with a group name goes to review.

The Reference screen also offers an LEI check (Excel): LEIs to fix (retired, merged, annulled, not found,
on two rows) and LEIs worth a look (name differs from every GLEIF name: renamed, or the wrong LEI).
Bank names are never sent to GLEIF.

Tested on real data (5,400-company group-level list): of 3,000 real GLEIF subsidiaries, 2,802 auto-matched
to the right group with no errors and 97.5% were found in auto + review; of 15,000 unrelated real
companies, 3 were auto-matched (all on inspection the same business) and ~5% went to review.

## Capital IQ export

Optional CSV: `ciq_id, name, aliases (';'-separated), country`. Names become variants for the
in-house entity carrying that CIQ ID. Check your licence covers this use.

## Adapting to what the bank sends

The bank file only needs a name column. Everything else is used when present, row by row:

| Bank supplies | What happens |
| --- | --- |
| LEI or CIQ ID | Exact ID match against the in-house list |
| Country | Country must agree for auto-match; same-name entities in other countries are separated |
| Name only | Legal form hints at country ("GmbH" cannot be a UK entity); the GLEIF name check sends names registered in several countries to review; one-word names never auto-match |
| Name only, domestic book | Set "Default country" on the match screen |

After each run the app shows what the file contained (e.g. "Name 100%, Country 0%").

## Master mapping (previous mappings)

Import a CSV of past human-verified mappings (bank's spelling, in-house ID, optional country
and bank code) on the Reference screen. It is checked first on every run, after the same country
and legal-form checks as any exact match; conflicting history goes to review. The spellings also
become fuzzy-match variants. Reviewer decisions are added automatically. Rebuild the reference
after importing so the new spellings reach fuzzy matching.

Measured on the synthetic names-only file: auto-matched rose from 63% to 83% when last quarter's
mapping for the same bank was loaded; 98.8% of true matches found in auto + review.

## Name collisions ("Apple" vs "Green Apple")

`tests/test_edge_cases.py` pins the behaviour: `python -m pytest tests -q`.

- Exact cleaned names win first, so "Apple Inc" and "Green Apple Ltd" never cross-match.
- Auto-match needs the distinctive words to be identical: "Green" must be on both sides.
- A one-word name ("Apple") auto-matches only when both sides have the same country.
- If every word the bank wrote appears in a longer in-house name ("Apple Bank" in "Apple Bank for
  Savings"), that candidate goes to review. Toggle: "containment review" in the match settings.
- Different numbers ("Fund 2" vs "Fund 3") never auto-match.

## Measuring accuracy

```bash
python scripts/evaluate.py inhouse.csv bank.csv --gleif gleif.csv --name-index --master master.csv
```

On the synthetic set (10k in-house, 100k bank rows, ~8.5k true matches) v0 gives:
auto-match precision 99.7%, 74% of true matches auto-matched, 98% found in auto + review,
review queue 8.9k rows (9% of the file), about 2 minutes on a laptop-class CPU.
Turning containment review off gives a smaller queue (~5k) but lets "Apple Bank" rank "Apple Inc" first.
These numbers are from synthetic data: recalibrate thresholds on a labelled sample of real files.

## Layout

```
entity_mapper/normalize.py   cleaning, legal forms, countries, LEI checksum
entity_mapper/gleif.py       Golden Copy subset loader
entity_mapper/matcher.py     reference index, tiers, scoring, banding
entity_mapper/store.py       DuckDB workspace: aliases, runs, audit
entity_mapper/shared.py      shared folder: published reference, master mapping, decision logs, merge rule
entity_mapper/sync.py        bring the local workspace in line with the shared folder
entity_mapper/gleif_fetch.py Golden Copy download (owner)
entity_mapper/universe_build.py  universe master build (owner; also used by the script)
api/server.py                FastAPI endpoints (upload, validate, startup, reference, match, review, owner)
run.py                       browser mode: starts the server and opens the browser
engine_main.py               desktop mode: the engine the Tauri shell starts
engine.spec, scripts/build_engine.py   package the engine with PyInstaller
src-tauri/                   Tauri shell (Rust): starts, watches and stops the engine
desktop/splash/              page shown while the engine starts
web/src/                     React UI (Start, Match, Review, Reference data, Alias memory, Owner)
scripts/                     sample data generator, evaluation, universe build
tests/                       matching edge cases, shared folder rules, end-to-end API flow
```
