"""Shared folder: one reference, one master mapping, everyone's decisions merged without a shared database."""
import json

import pandas as pd
import pytest

from entity_mapper.shared import SharedFolder, local_reference, merge_decisions
from entity_mapper.store import Workspace
from entity_mapper.sync import sync_shared


def decide(ws, name, inhouse_id, bank="BARC", country="GB"):
    ws.save_decisions(bank, "run1", pd.DataFrame([{
        "bank_row_id": "1", "bank_name": name, "bank_country": country, "clean_name": name.lower(),
        "status": "review", "inhouse_id": "", "decision_inhouse_id": inhouse_id}]))


@pytest.fixture
def two_users(tmp_path, monkeypatch):
    shared = SharedFolder(tmp_path / "shared")
    shared.path.mkdir()
    spaces = {u: Workspace(tmp_path / f"{u}.duckdb") for u in ("alice", "bob")}

    def as_user(u):
        monkeypatch.setenv("ENTITY_MAPPER_USER", u)
        return spaces[u]
    return shared, as_user


def test_decisions_travel_between_users(two_users):
    shared, as_user = two_users
    decide(as_user("alice"), "Acme Ltd", "C1")
    assert sync_shared(as_user("alice"), shared)["pushed"] == 1
    out = sync_shared(as_user("bob"), shared)
    assert out["decisions"] == 1 and out["conflicts"] == []
    assert as_user("bob").aliases_for("BARC") == {("acme ltd", "GB"): "C1"}


def test_sync_is_repeatable(two_users):
    shared, as_user = two_users
    decide(as_user("alice"), "Acme Ltd", "C1")
    for _ in range(3):
        sync_shared(as_user("alice"), shared)
    assert len(shared.read_decisions()) == 1


def test_disagreement_is_a_conflict_until_the_owner_decides(two_users):
    shared, as_user = two_users
    shared.set_owner("alice")
    decide(as_user("bob"), "Acme Ltd", "C1")
    sync_shared(as_user("bob"), shared)
    monkey_other = Workspace(shared.path.parent / "carol.duckdb")
    import os
    os.environ["ENTITY_MAPPER_USER"] = "carol"
    decide(monkey_other, "Acme Ltd", "C2")
    out = sync_shared(monkey_other, shared)
    assert len(out["conflicts"]) == 1
    assert {o["inhouse_id"] for o in out["conflicts"][0]["options"]} == {"C1", "C2"}
    # the owner settles it: no conflict left, and the owner's choice reaches everyone
    alice = as_user("alice")
    sync_shared(alice, shared)
    alice.set_alias("BARC", "acme ltd", "GB", "C1", "Acme Ltd")
    assert sync_shared(alice, shared)["conflicts"] == []
    assert sync_shared(as_user("bob"), shared)["conflicts"] == []
    os.environ["ENTITY_MAPPER_USER"] = "carol"
    sync_shared(monkey_other, shared)
    assert monkey_other.aliases_for("BARC") == {("acme ltd", "GB"): "C1"}


def test_latest_wins_without_owner_and_no_match_is_kept():
    recs = [
        {"bank_code": "B", "name_clean": "x", "country": "", "inhouse_id": "C1", "decided_by": "u1", "decided_at": "2026-01-01T10:00:00"},
        {"bank_code": "B", "name_clean": "x", "country": "", "inhouse_id": "__NO_MATCH__", "decided_by": "u1", "decided_at": "2026-02-01T10:00:00"},
    ]
    winners, conflicts = merge_decisions(recs)
    assert winners[0]["inhouse_id"] == "__NO_MATCH__" and conflicts == []  # one user changing their mind is not a conflict


def test_read_only_share_keeps_decisions_local(two_users, monkeypatch):
    shared, as_user = two_users
    ws = as_user("alice")
    decide(ws, "Acme Ltd", "C1")
    monkeypatch.setattr(SharedFolder, "append_decisions", lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only")))
    out = sync_shared(ws, shared)
    assert out["writable"] is False and out["decisions"] == 1
    assert ws.aliases_for("BARC") == {("acme ltd", "GB"): "C1"}


def test_half_written_line_is_skipped(two_users):
    shared, as_user = two_users
    decide(as_user("alice"), "Acme Ltd", "C1")
    sync_shared(as_user("alice"), shared)
    with open(shared.path / "decisions" / "alice.jsonl", "a") as f:
        f.write('{"bank_code": "BARC", "name_cl')
    assert len(shared.read_decisions()) == 1


def test_master_mapping_is_shared(two_users):
    shared, as_user = two_users
    alice = as_user("alice")
    alice.import_master(pd.DataFrame({"name": ["Acme Limited"], "inhouse_id": ["c1"]}), source="q1.csv")
    sync_shared(alice, shared)  # first user seeds the shared file
    assert sync_shared(as_user("bob"), shared)["master_rows"] == 1
    assert as_user("bob").master_df()["inhouse_id"].tolist() == ["C1"]


def test_reference_publish_and_sync(two_users, tmp_path):
    shared, as_user = two_users
    as_user("alice")
    f = tmp_path / "um.xlsx"
    pd.DataFrame({"role": ["group"]}).to_excel(f, sheet_name="universe master", index=False)
    m1 = shared.publish_reference(f)
    assert shared.owner == "alice"
    local = tmp_path / "bob_ref"
    path, manifest, copied = shared.sync_reference(local)
    assert copied and path.exists() and manifest["sha256"] == m1["sha256"]
    assert shared.sync_reference(local)[2] is False          # nothing new: no copy
    pd.DataFrame({"role": ["group", "subsidiary"]}).to_excel(f, sheet_name="universe master", index=False)
    shared.publish_reference(f)
    path2, manifest2, copied2 = shared.sync_reference(local)
    assert copied2 and manifest2["sha256"] != m1["sha256"] and not path.exists()
    # offline: the last copy is still usable
    assert local_reference(local)[0] == path2
    assert json.loads((local / "manifest.json").read_text())["published_by"] == "alice"
