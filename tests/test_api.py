"""Visible tests for Part 5. They must all pass. Add your own tests too (a few
extra tests of your own are valued more than a perfect score here)."""
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import RunRepository, app, get_repo

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture()
def client():
    repo = RunRepository()
    app.dependency_overrides[get_repo] = lambda: repo
    yield TestClient(app)
    app.dependency_overrides.clear()


# --- a tiny plate whose answer can be checked by hand -----------------------
# blanks=10,10 | neg controls net = 10,12,8,10 -> mean 10, sd(ddof=1)=1.633
# cutoff = 10 + 3*1.633 = 14.899
SAMPLES = [
    {"sample_id": "BLK1", "well": "A1", "sample_type": "blank"},
    {"sample_id": "BLK2", "well": "B1", "sample_type": "blank"},
    {"sample_id": "NEG1", "well": "A2", "sample_type": "neg_control"},
    {"sample_id": "NEG2", "well": "B2", "sample_type": "neg_control"},
    {"sample_id": "NEG3", "well": "C2", "sample_type": "neg_control"},
    {"sample_id": "NEG4", "well": "D2", "sample_type": "neg_control"},
    {"sample_id": "POS1", "well": "E2", "sample_type": "pos_control"},
    {"sample_id": "PAT_A", "well": "F2", "sample_type": "patient"},
    {"sample_id": "PAT_B", "well": "G2", "sample_type": "patient"},
    {"sample_id": "PAT_C", "well": "H2", "sample_type": "patient"},
]


def _reading(well, mfi, beads=90, analyte="AN_X"):
    return {"well": well, "analyte": analyte, "mfi": mfi, "bead_count": beads}


READINGS = [
    _reading("A1", 10), _reading("B1", "10"),            # str numbers must work
    _reading("A2", 20), _reading("B2", "22"), _reading("C2", 18), _reading("D2", 20),
    _reading("E2", 500),
    _reading("f2", 800),                                 # lower-case well
    _reading("G2", "21,0"),                              # decimal comma
    _reading("H2", 300, beads=10),                       # too few beads
    _reading("A1", 10),                                  # exact duplicate
]


def _make_run(client, samples=SAMPLES):
    r = client.post("/runs", json={"name": "demo", "samples": samples})
    assert r.status_code == 201, r.text
    return r.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get_run(client):
    run = _make_run(client)
    assert run["name"] == "demo" and run["n_samples"] == 10 and run["n_readings"] == 0
    again = client.get(f"/runs/{run['id']}")
    assert again.status_code == 200 and again.json() == run


def test_ids_are_distinct(client):
    assert _make_run(client)["id"] != _make_run(client)["id"]


def test_missing_run_is_404(client):
    assert client.get("/runs/999").status_code == 404
    assert client.post("/runs/999/readings", json=[]).status_code == 404
    assert client.get("/runs/999/results").status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "x", "samples": [{"sample_id": "a", "well": "I1"}]},                 # bad row
        {"name": "x", "samples": [{"sample_id": "a", "well": "A13"}]},                # bad col
        {"name": "x", "samples": []},                                                 # empty
        {"name": "", "samples": [{"sample_id": "a", "well": "A1"}]},                  # no name
        {"name": "x", "samples": [{"sample_id": "a", "well": "A1"},
                                  {"sample_id": "b", "well": "a1"}]},                 # same well
        {"name": "x", "samples": [{"sample_id": "a", "well": "A1"},
                                  {"sample_id": "a", "well": "A2"}]},                 # same id
    ],
)
def test_create_run_validation(client, bad):
    assert client.post("/runs", json=bad).status_code == 422


def test_results_before_readings_is_409(client):
    run = _make_run(client)
    assert client.get(f"/runs/{run['id']}/results").status_code == 409


def test_add_readings_counts_rows(client):
    run = _make_run(client)
    r = client.post(f"/runs/{run['id']}/readings", json=READINGS)
    assert r.status_code == 200 and r.json() == {"added": len(READINGS)}
    assert client.get(f"/runs/{run['id']}").json()["n_readings"] == len(READINGS)


def test_results_hand_checked(client):
    run = _make_run(client)
    client.post(f"/runs/{run['id']}/readings", json=READINGS)
    body = client.get(f"/runs/{run['id']}/results").json()

    assert body["run_valid"] is True
    assert body["cutoffs"]["AN_X"] == pytest.approx(14.899, abs=1e-3)

    calls = {c["sample_id"]: c for c in body["calls"]}
    assert len(body["calls"]) == 10                       # duplicate was dropped
    assert calls["PAT_A"]["call"] == "POS" and calls["PAT_A"]["net_mfi"] == pytest.approx(790)
    assert calls["PAT_B"]["call"] == "NEG" and calls["PAT_B"]["net_mfi"] == pytest.approx(11)
    assert calls["PAT_C"]["call"] == "INVALID"            # only 10 beads
    assert calls["POS1"]["call"] == "POS"


def test_run_is_invalid_when_positive_control_fails(client):
    run = _make_run(client)
    bad = [r if r["well"] != "E2" else _reading("E2", 12) for r in READINGS]
    client.post(f"/runs/{run['id']}/readings", json=bad)
    body = client.get(f"/runs/{run['id']}/results").json()
    assert body["run_valid"] is False


def test_cutoff_is_none_without_enough_negative_controls(client):
    run = _make_run(client)
    only_one_neg = [r for r in READINGS if r["well"] not in ("B2", "C2", "D2")]
    client.post(f"/runs/{run['id']}/readings", json=only_one_neg)
    body = client.get(f"/runs/{run['id']}/results").json()
    assert body["cutoffs"]["AN_X"] is None
    assert body["run_valid"] is False
    assert all(c["call"] == "INVALID" for c in body["calls"])


def test_full_plate_from_data_folder(client):
    layout = pd.read_csv(DATA / "plate_layout.csv")
    raw = pd.read_csv(DATA / "readings_raw.csv", dtype={"mfi": str}, keep_default_na=False)
    samples = layout[["sample_id", "well", "sample_type"]].to_dict("records")
    run = _make_run(client, samples)
    rows = [
        {**r, "mfi": (r["mfi"] if r["mfi"] != "" else None), "bead_count": int(r["bead_count"])}
        for r in raw.to_dict("records")
    ]
    assert client.post(f"/runs/{run['id']}/readings", json=rows).status_code == 200
    body = client.get(f"/runs/{run['id']}/results").json()
    assert len(body["calls"]) == 96 * 4                   # 384 unique (well, analyte)
    assert set(body["cutoffs"]) == {"AN_A", "AN_B", "AN_C", "AN_D"}
    assert body["run_valid"] is True
    assert any(c["call"] == "POS" for c in body["calls"])
    assert any(c["call"] == "INVALID" for c in body["calls"])

def test_readings_accumulate_across_uploads(client):
    run = _make_run(client)
    first, second = READINGS[:5], READINGS[5:]

    client.post(f"/runs/{run['id']}/readings", json=first)
    r = client.post(f"/runs/{run['id']}/readings", json=second)

    assert r.json() == {"added": len(second)}
    assert client.get(f"/runs/{run['id']}").json()["n_readings"] == len(READINGS)
    body = client.get(f"/runs/{run['id']}/results").json()
    assert body["run_valid"] is True and len(body["calls"]) == 10


def test_unreadable_mfi_is_invalid_with_null_net(client):
    run = _make_run(client)
    readings = [r if r["well"] != "G2" else _reading("G2", "OOR >") for r in READINGS]
    client.post(f"/runs/{run['id']}/readings", json=readings)

    body = client.get(f"/runs/{run['id']}/results").json()
    calls = {c["sample_id"]: c for c in body["calls"]}

    assert calls["PAT_B"]["call"] == "INVALID"
    assert calls["PAT_B"]["net_mfi"] is None
    assert body["run_valid"] is True


def test_reading_for_well_outside_the_run_is_ignored(client):
    run = _make_run(client)
    extra = READINGS + [_reading("H12", 999)]      # H12 não está em SAMPLES

    r = client.post(f"/runs/{run['id']}/readings", json=extra)
    assert r.json() == {"added": len(extra)}

    body = client.get(f"/runs/{run['id']}/results").json()
    assert len(body["calls"]) == 10
    assert "H12" not in {c["well"] for c in body["calls"]}