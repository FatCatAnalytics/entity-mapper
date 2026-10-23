"""Download the GLEIF Golden Copy (owner path only).

Public data, fetched with the standard library so the operating system's certificate store is used
(a corporate proxy's root certificate is normally installed there). Nothing is sent except the request.
Downloads resume: an interrupted file continues from where it stopped.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

LATEST = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
UA = {"User-Agent": "EntityMapper/1.0", "Accept": "application/json"}


def latest_publish(timeout: int = 30) -> dict:
    """{'publish_date', 'lei2': {'url','size','name'}, 'rr': {...}} for the newest full files (CSV)."""
    with urllib.request.urlopen(urllib.request.Request(LATEST, headers=UA), timeout=timeout) as r:
        data = json.load(r)["data"]
    out = {"publish_date": data["publish_date"][:10]}
    for kind in ("lei2", "rr"):
        f = data[kind]["full_file"]["csv"]
        out[kind] = {"url": f["url"], "size": int(f["size"]), "name": f["url"].rsplit("/", 1)[-1],
                     "records": f.get("record_count")}
    return out


def download(url: str, dest: Path, size: int | None = None,
             progress: Callable[[int, int | None], None] | None = None, timeout: int = 60) -> Path:
    """Stream `url` to `dest`, resuming a partial download. Returns dest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (size is None or dest.stat().st_size == size):
        return dest
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have > size:
        part.unlink()
        have = 0
    if not (size and have == size):
        headers = {"User-Agent": UA["User-Agent"]}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 416:  # 416: the part file is already complete
                raise
            resp = None
        if resp is not None:
            with resp:
                if have and resp.status != 206:  # server ignored the range: start again
                    have = 0
                with open(part, "ab" if have else "wb") as f:
                    while True:
                        block = resp.read(1 << 20)
                        if not block:
                            break
                        f.write(block)
                        have += len(block)
                        if progress:
                            progress(have, size)
    if size and part.stat().st_size != size:
        raise OSError(f"Download incomplete: {part.stat().st_size:,} of {size:,} bytes. Run it again to resume.")
    part.replace(dest)
    return dest


def fetch_golden_copy(folder: Path, progress: Callable[[str, int, int | None], None] | None = None) -> dict:
    """Download the latest Level 1 and Level 2 files into `folder`; older copies there are removed."""
    folder = Path(folder)
    pub = latest_publish()
    paths = {}
    for kind in ("rr", "lei2"):
        meta = pub[kind]
        paths[kind] = download(meta["url"], folder / meta["name"], meta["size"],
                               (lambda done, total, k=kind: progress(k, done, total)) if progress else None)
    keep = {p.name for p in paths.values()}
    for old in folder.glob("*-gleif-goldencopy-*-golden-copy.csv.zip*"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return {"publish_date": pub["publish_date"], "lei2": paths["lei2"], "rr": paths["rr"]}
