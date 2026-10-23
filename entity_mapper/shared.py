"""Shared folder: one published reference, one master mapping and everyone's decisions.

Layout on the shared drive (a plain folder; no database file is ever opened there):

    <shared>/config.json                     {"owner": "<username>"}
    <shared>/reference/manifest.json         which universe master file is current
    <shared>/reference/universe_master_*.xlsx
    <shared>/master/master_mapping.csv       previous mappings, written whole by whoever imports
    <shared>/decisions/<username>.jsonl      append-only; each user writes only their own file

Why files and not a shared database: DuckDB allows one writing process, and SQLite's own documentation
warns against network drives. Appending to a file that only you write cannot collide with anyone.
The app merges all logs on start: the owner's decision wins, otherwise the latest; disagreements
between users are listed for the owner.

Client names appear in the decision logs, so the shared folder must be an internal drive.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

MASTER_COLS = ["name", "country", "inhouse_id", "bank_code", "source", "imported_at"]
KEY = ("bank_code", "name_clean", "country")


def current_user() -> str:
    return os.environ.get("ENTITY_MAPPER_USER") or getpass.getuser()


def now_utc() -> datetime:
    """Naive UTC, so 'latest decision wins' compares the same clock for every user."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "user"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------- settings (per user, local) ----------
class Settings:
    """<home>/settings.json. ENTITY_MAPPER_SHARED overrides the saved shared folder."""

    def __init__(self, home: Path):
        self.path = Path(home) / "settings.json"

    def load(self) -> dict:
        return _read_json(self.path) or {}

    def save(self, **changes) -> dict:
        data = {**self.load(), **changes}
        _write_atomic(self.path, json.dumps(data, indent=2).encode("utf-8"))
        return data

    @property
    def shared_folder(self) -> str:
        return os.environ.get("ENTITY_MAPPER_SHARED") or self.load().get("shared_folder") or ""


# ---------- decision records ----------
def decision_id(r: dict) -> str:
    raw = "|".join(str(r.get(k) or "") for k in
                   ("bank_code", "name_clean", "country", "inhouse_id", "decided_by", "decided_at"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def merge_decisions(records: list[dict], owner: str | None = None) -> tuple[list[dict], list[dict]]:
    """Every user's decisions -> (one winning decision per key, conflicts for the owner).

    Key = bank code + cleaned name + country. Per key: the owner's latest decision wins if there is one;
    otherwise the latest decision wins and, if users' latest decisions disagree, the key is a conflict.
    """
    latest: dict[tuple, dict[str, dict]] = {}
    for r in records:
        key = tuple(r.get(k) or "" for k in KEY)
        per_user = latest.setdefault(key, {})
        user = r.get("decided_by") or ""
        if user not in per_user or str(r.get("decided_at")) >= str(per_user[user].get("decided_at")):
            per_user[user] = r
    winners, conflicts = [], []
    for key, per_user in latest.items():
        ordered = sorted(per_user.values(), key=lambda r: str(r.get("decided_at")), reverse=True)
        if owner and owner in per_user:
            winners.append(per_user[owner])
            continue
        winners.append(ordered[0])
        if len({r.get("inhouse_id") for r in ordered}) > 1:
            conflicts.append({"bank_code": key[0], "name_clean": key[1], "country": key[2],
                              "raw_name": ordered[0].get("raw_name") or key[1],
                              "current": ordered[0].get("inhouse_id"),
                              "options": [{"inhouse_id": r.get("inhouse_id"), "decided_by": r.get("decided_by"),
                                           "decided_at": str(r.get("decided_at"))} for r in ordered]})
    return winners, conflicts


def local_reference(local_dir: Path) -> tuple[Path | None, dict | None]:
    """The reference copied to this machine earlier, if any (used when the shared folder is offline)."""
    local = _read_json(Path(local_dir) / "manifest.json")
    if local and (Path(local_dir) / local["file"]).exists():
        return Path(local_dir) / local["file"], local
    return None, None


# ---------- the shared folder ----------
class SharedFolder:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    # -- access --
    def reachable(self) -> bool:
        try:
            return self.path.is_dir()
        except OSError:
            return False

    def writable(self) -> bool:
        try:
            probe = self.path / f".write_test_{_safe(current_user())}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    @property
    def owner(self) -> str:
        return (_read_json(self.path / "config.json") or {}).get("owner", "")

    def set_owner(self, user: str) -> None:
        cfg = _read_json(self.path / "config.json") or {}
        _write_atomic(self.path / "config.json", json.dumps({**cfg, "owner": user}, indent=2).encode("utf-8"))

    # -- reference --
    def reference_manifest(self) -> dict | None:
        return _read_json(self.path / "reference" / "manifest.json")

    def publish_reference(self, xlsx: Path, info: dict | None = None) -> dict:
        """Copy a universe master file into the shared folder and make it the current one."""
        user, stamp = current_user(), now_utc()
        digest = sha256(xlsx)
        name = f"universe_master_{stamp:%Y%m%d-%H%M}_{digest[:8]}.xlsx"
        dest = self.path / "reference" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(xlsx, dest)
        if sha256(dest) != digest:
            dest.unlink(missing_ok=True)
            raise OSError("The copy on the shared folder does not match the file; nothing was published.")
        manifest = {"file": name, "sha256": digest, "version": f"{stamp:%Y-%m-%d %H:%M} UTC",
                    "published_at": stamp.isoformat(), "published_by": user, **(info or {})}
        _write_atomic(dest.parent / "manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
        if not self.owner:
            self.set_owner(user)
        return manifest

    def sync_reference(self, local_dir: Path) -> tuple[Path | None, dict | None, bool]:
        """Make the local copy match the published one. Returns (local file, manifest, copied?)."""
        local_dir.mkdir(parents=True, exist_ok=True)
        local = _read_json(local_dir / "manifest.json")
        published = self.reference_manifest() if self.reachable() else None
        if published and (not local or local.get("sha256") != published.get("sha256")
                          or not (local_dir / local["file"]).exists()):
            src = self.path / "reference" / published["file"]
            tmp = local_dir / (published["file"] + ".part")
            shutil.copyfile(src, tmp)
            if sha256(tmp) != published["sha256"]:
                tmp.unlink(missing_ok=True)
                raise OSError("The reference file changed while it was being copied; try again.")
            os.replace(tmp, local_dir / published["file"])
            _write_atomic(local_dir / "manifest.json", json.dumps(published, indent=2).encode("utf-8"))
            for old in local_dir.glob("universe_master_*.xlsx"):
                if old.name != published["file"]:
                    old.unlink(missing_ok=True)
            return local_dir / published["file"], published, True
        if local and (local_dir / local["file"]).exists():
            return local_dir / local["file"], local, False
        return None, None, False

    # -- master mapping --
    @property
    def master_path(self) -> Path:
        return self.path / "master" / "master_mapping.csv"

    def read_master(self) -> pd.DataFrame:
        if not self.master_path.exists():
            return pd.DataFrame(columns=MASTER_COLS)
        df = pd.read_csv(self.master_path, dtype=str, keep_default_na=False)
        return df.reindex(columns=MASTER_COLS, fill_value="")

    def write_master(self, incoming: pd.DataFrame, replace: bool = False) -> int:
        """incoming: MASTER_COLS. Appends (dropping exact repeats) or replaces; written in one move."""
        incoming = incoming.reindex(columns=MASTER_COLS, fill_value="").astype(str)
        df = incoming if replace else pd.concat([self.read_master(), incoming], ignore_index=True)
        df = df.drop_duplicates(subset=["name", "country", "inhouse_id", "bank_code"], keep="last")
        _write_atomic(self.master_path, df.to_csv(index=False).encode("utf-8"))
        return len(df)

    # -- decisions --
    def _log(self, user: str) -> Path:
        return self.path / "decisions" / f"{_safe(user)}.jsonl"

    def append_decisions(self, records: list[dict], user: str | None = None) -> int:
        if not records:
            return 0
        path = self._log(user or current_user())
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps({**r, "id": r.get("id") or decision_id(r)}, ensure_ascii=False) + "\n")
        return len(records)

    def read_decisions(self, user: str | None = None) -> list[dict]:
        """All users' decisions, or one user's."""
        out = []
        folder = self.path / "decisions"
        if not folder.is_dir():
            return out
        for path in ([self._log(user)] if user else sorted(folder.glob("*.jsonl"))):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:  # a line still being written by its owner is skipped until the next sync
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out
