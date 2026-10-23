"""The whole desktop flow through the API: owner builds and publishes, a user starts up and matches."""
import io
import os
import tempfile
import time
import zipfile
from pathlib import Path

import pandas as pd
import pytest

_TMP = Path(tempfile.mkdtemp(prefix="em_flow_"))
os.environ["ENTITY_MAPPER_HOME"] = str(_TMP / "home")
os.environ["ENTITY_MAPPER_USER"] = "owner1"
os.environ.pop("ENTITY_MAPPER_SHARED", None)

from fastapi.testclient import TestClient  # noqa: E402

from api import server  # noqa: E402

client = TestClient(server.app)


def lei(base: str) -> str:
    base = base.upper().ljust(18, "0")[:18]
    digits = "".join(str(int(c, 36)) for c in base + "00")
    return base + f"{98 - int(digits) % 97:02d}"


ACME, ACME_SUB, BOLT = lei("ACME"), lei("ACMESUB"), lei("BOLT")


def wait(job_id):
    for _ in range(600):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["done"]:
            assert j["error"] is None, j["error"]
            return j["result"]
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def upload(name, content: bytes, kind, **params):
    r = client.post("/api/upload", params={"kind": kind, **params}, files={"file": (name, content)})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def gleif_files():
    l1 = pd.DataFrame({
        "LEI": [ACME, ACME_SUB, BOLT],
        "Entity.LegalName": ["Acme Holdings PLC", "Acme Treasury Services Limited", "Bolt Industries GmbH"],
        "Entity.LegalAddress.Country": ["GB", "IE", "DE"], "Entity.LegalAddress.City": ["London", "Dublin", "Munich"],
        "Entity.EntityStatus": ["ACTIVE"] * 3, "Registration.RegistrationStatus": ["ISSUED"] * 3})
    rr = pd.DataFrame({"Relationship.StartNode.NodeID": [ACME_SUB], "Relationship.EndNode.NodeID": [ACME],
                       "Relationship.RelationshipType": ["IS_DIRECTLY_CONSOLIDATED_BY"],
                       "Relationship.RelationshipStatus": ["ACTIVE"]})
    out = []
    for name, df in (("lei2.csv.zip", l1), ("rr.csv.zip", rr)):
        p = _TMP / name
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(name[:-4], df.to_csv(index=False))
        out.append(str(p))
    return out


def test_1_nothing_configured_yet():
    s = client.get("/api/startup").json()
    assert s["shared"]["configured"] is False and s["reference"]["loaded"] is None
    assert wait(client.post("/api/startup/sync").json()["job_id"])["reference_loaded"] is False


def test_2_shared_folder_must_exist():
    assert client.post("/api/settings", json={"shared_folder": str(_TMP / "nope")}).status_code == 400
    (_TMP / "shared").mkdir()
    s = client.post("/api/settings", json={"shared_folder": str(_TMP / "shared")}).json()
    assert s["shared"]["reachable"] is True and s["reference"]["published"] is None


def test_3_owner_builds_and_publishes(gleif_files):
    assert client.post("/api/owner/publish", json={}).status_code == 400      # nothing built yet
    uni = pd.DataFrame({"Coalition ID": ["C1", "C2"], "Coalition Name": ["Acme Holdings", "Bolt Industries"],
                        "Country": ["GB", "DE"], "LEI": [ACME, BOLT]})
    up = upload("universe.csv", uni.to_csv(index=False).encode(), "inhouse")
    assert up["suggested"]["inhouse_id"] == "Coalition ID" and up["suggested"]["lei"] == "LEI"
    r = client.post("/api/owner/build", json={"inhouse_file_id": up["file_id"], "mapping": up["suggested"],
                                              "lei2_path": gleif_files[0]})
    assert r.status_code == 400                                                 # one GLEIF file is not enough
    r = client.post("/api/owner/build", json={"inhouse_file_id": up["file_id"], "mapping": up["suggested"],
                                              "lei2_path": gleif_files[0], "rr_path": gleif_files[1]})
    built = wait(r.json()["job_id"])
    assert {"label": "Subsidiaries", "value": "1", "sub": False} in built["summary"]
    assert client.get("/api/owner/build/file").status_code == 200
    m = client.post("/api/owner/publish", json={}).json()
    assert m["published_by"] == "owner1" and m["roles"] == {"group": 2, "subsidiary": 1}
    s = client.get("/api/startup").json()
    assert s["shared"]["is_owner"] and s["reference"]["update_available"]


def test_4_startup_loads_the_published_reference():
    out = wait(client.post("/api/startup/sync").json()["job_id"])
    assert out["copied"] and out["reference_loaded"]
    s = client.get("/api/startup").json()
    assert s["reference"]["loaded"]["inhouse_entities"] == 2
    assert s["reference"]["update_available"] is False
    t = time.time()
    again = wait(client.post("/api/startup/sync").json()["job_id"])                 # nothing changed: no reload
    assert again["copied"] is False and time.time() - t < 2


def test_5_paste_validate_match_and_share_decisions():
    pasted = ("Client Name\tCountry\tLEI\n"
              "Acme Treasury Services Ltd\tIE\t\n"          # GLEIF subsidiary of C1
              "Bolt Industries GmbH\tDE\t5.49E+19\n"         # LEI mangled by a spreadsheet
              "\tGB\t\n"                                     # no name
              "Zebra Unrelated Trading Co\tGB\t\n")
    up = upload("pasted.tsv", pasted.encode(), "bank")
    assert up["rows"] == 4 and up["suggested"]["name"] == "Client Name"
    v = client.post("/api/validate", json={"file_id": up["file_id"], "mapping": up["suggested"]}).json()
    texts = " | ".join(i["text"] for i in v["issues"])
    assert v["ok"] is False and "without a name" in texts and "scientific notation" in texts

    no_header = upload("pasted.tsv", b"Acme Holdings PLC\tGB\n", "bank", header=0)
    assert no_header["columns"] == ["Column 1", "Column 2"] and no_header["rows"] == 1

    run = client.post("/api/match", json={"bank_file_id": up["file_id"], "mapping": up["suggested"],
                                          "bank_code": "barc"}).json()
    for _ in range(600):
        st = client.get(f"/api/runs/{run['run_id']}").json()
        if st["stage"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["stage"] == "done", st
    rows = {r["bank_name"]: r for r in client.get(f"/api/runs/{run['run_id']}/results",
                                                  params={"status": "all"}).json()["rows"]}
    assert rows["Acme Treasury Services Ltd"]["inhouse_id"] == "C1"
    assert rows["Bolt Industries GmbH"]["inhouse_id"] == "C2"
    zebra = rows["Zebra Unrelated Trading Co"]
    assert zebra["status"] == "no_match"
    d = client.post(f"/api/runs/{run['run_id']}/decisions",
                    json=[{"bank_row_id": zebra["bank_row_id"], "inhouse_id": "C2"}]).json()
    assert d["saved"] == 1 and d["shared"] is True
    log = (_TMP / "shared" / "decisions" / "owner1.jsonl").read_text().splitlines()
    assert len(log) == 1 and '"inhouse_id": "C2"' in log[0]


def test_6_master_import_reaches_the_shared_folder():
    up = upload("history.csv", b"bank_name,inhouse_id\nAcme Hldgs,C1\n", "master")
    out = client.post("/api/master/import", json={"file_id": up["file_id"], "mapping": up["suggested"],
                                                  "replace": False}).json()
    assert out["imported"] == 1 and "shared folder" in out["shared_note"]
    shared_master = pd.read_csv(_TMP / "shared" / "master" / "master_mapping.csv", dtype=str)
    assert shared_master["name"].tolist() == ["Acme Hldgs"] and shared_master["source"].tolist() == ["history.csv"]


def test_7_only_the_owner_publishes(monkeypatch):
    monkeypatch.setenv("ENTITY_MAPPER_USER", "someone_else")
    assert client.post("/api/owner/publish", json={}).status_code == 403
    assert client.post("/api/shared/resolve", json={"bank_code": "B", "name_clean": "x"}).status_code == 403


def test_8_offline_uses_the_last_copy(monkeypatch):
    server.STATE["ref"], server.STATE["ref_info"] = None, None
    monkeypatch.setenv("ENTITY_MAPPER_SHARED", str(_TMP / "unplugged"))
    out = wait(client.post("/api/startup/sync").json()["job_id"])
    assert out["offline"] is True and out["reference_loaded"] is True
