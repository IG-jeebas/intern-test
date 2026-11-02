"""Part 5 - FastAPI service (skeleton).

Run it:   uvicorn app.main:app --reload
Docs:     http://127.0.0.1:8000/docs
Tests:    pytest -q

Endpoints you must build (see README for the full contract):
    GET  /health                    (given)
    POST /runs                      -> 201 RunOut
    GET  /runs/{run_id}             -> RunOut      | 404
    POST /runs/{run_id}/readings    -> {"added": n}| 404
    GET  /runs/{run_id}/results     -> ResultsOut  | 404 | 409 (no readings yet)

Do NOT rename `get_repo` - the tests replace it with a fresh repository.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app import analysis
from app.schemas import CallOut, ReadingIn, ResultsOut, RunCreate, RunOut, SampleIn


@dataclass
class StoredRun:
    name: str
    samples: List[SampleIn]
    readings: List[dict] = field(default_factory=list)


class RunRepository:
    """In-memory storage for runs."""

    def __init__(self) -> None:
        self._runs: Dict[int, StoredRun] = {}
        self._next_id = 1

    def create(self, payload: RunCreate) -> int:
        run_id = self._next_id
        self._next_id += 1
        self._runs[run_id] = StoredRun(name=payload.name, samples=list(payload.samples))
        return run_id

    def get(self, run_id: int) -> Optional[StoredRun]:
        return self._runs.get(run_id)

    def add_readings(self, run_id: int, readings: List[ReadingIn]) -> int:
        self._runs[run_id].readings.extend(r.model_dump() for r in readings)
        return len(readings)


_repo = RunRepository()


def get_repo() -> RunRepository:
    return _repo


app = FastAPI(title="Intern Test API", version="0.1.0")


def _get_run_or_404(repo: RunRepository, run_id: int) -> StoredRun:
    run = repo.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return run


def _run_out(run_id: int, run: StoredRun) -> RunOut:
    return RunOut(id=run_id, name=run.name,
                  n_samples=len(run.samples), n_readings=len(run.readings))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/runs", response_model=RunOut, status_code=201)
def create_run(payload: RunCreate, repo: RunRepository = Depends(get_repo)) -> RunOut:
    run_id = repo.create(payload)
    return _run_out(run_id, repo.get(run_id))


@app.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, repo: RunRepository = Depends(get_repo)) -> RunOut:
    return _run_out(run_id, _get_run_or_404(repo, run_id))


@app.post("/runs/{run_id}/readings")
def add_readings(run_id: int, readings: List[ReadingIn],
                 repo: RunRepository = Depends(get_repo)) -> dict:
    _get_run_or_404(repo, run_id)
    return {"added": repo.add_readings(run_id, readings)}


@app.get("/runs/{run_id}/results", response_model=ResultsOut)
def get_results(run_id: int, repo: RunRepository = Depends(get_repo)) -> ResultsOut:
    run = _get_run_or_404(repo, run_id)
    if not run.readings:
        raise HTTPException(status_code=409, detail="no readings uploaded yet")

    layout = pd.DataFrame(
        [{"sample_id": s.sample_id, "well": s.well, "sample_type": s.sample_type.value}
         for s in run.samples]
    )
    results, cutoffs, run_valid = analysis.analyze(layout, pd.DataFrame(run.readings))

    calls = [
        CallOut(
            well=r.well, sample_id=r.sample_id, sample_type=r.sample_type,
            analyte=r.analyte,
            net_mfi=None if pd.isna(r.net_mfi) else float(r.net_mfi),
            call=r.call,
        )
        for r in results.itertuples()
    ]
    return ResultsOut(run_id=run_id, run_valid=run_valid, cutoffs=cutoffs, calls=calls)


# Bonus (optional): a tiny front-end served from ./static at http://127.0.0.1:8000/ui
_static = Path(__file__).resolve().parent.parent / "static"
if _static.exists():
    app.mount("/ui", StaticFiles(directory=_static, html=True), name="ui")