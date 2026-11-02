"""Part 5 - the analysis logic, as importable functions.

In Part 3 you built this inside the notebook. Here you move it into a module so
the API can reuse it. The rules are the ones written in the notebook, Part 3
("Calling rules"). Keep them identical.

Expected shapes
---------------
layout   : DataFrame with columns  well, sample_id, sample_type
readings : DataFrame with columns  well, analyte, mfi, bead_count      (RAW / messy)
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

MIN_BEADS = 35


def clean_readings(readings: pd.DataFrame) -> pd.DataFrame:
    """Normalise well names, make `mfi` numeric (NaN when unusable) and drop
    exact duplicate rows. Must not modify the input DataFrame."""
    df = readings.copy()
    df["well"] = df["well"].astype("string").str.strip().str.upper()
    df["mfi"] = df["mfi"].astype("string").str.strip().str.replace(",", ".", regex=False)
    df["mfi"] = pd.to_numeric(df["mfi"], errors="coerce").astype(float)
    return df.drop_duplicates().reset_index(drop=True)


def analyze(
    layout: pd.DataFrame, readings: pd.DataFrame
) -> Tuple[pd.DataFrame, Dict[str, Optional[float]], bool]:
    df = clean_readings(readings).merge(
        layout[["well", "sample_id", "sample_type"]], on="well", how="inner"
    )
    analytes = sorted(df["analyte"].unique())

    blank_mean = (
        df[df["sample_type"] == "blank"].groupby("analyte")["mfi"].mean().reindex(analytes)
    )
    df["net_mfi"] = df["mfi"] - df["analyte"].map(blank_mean)

    df["valid"] = df["mfi"].notna() & (df["bead_count"] >= MIN_BEADS)

    neg = (
        df[(df["sample_type"] == "neg_control") & df["valid"]]
        .groupby("analyte")["net_mfi"]
    )
    cutoff_series = (
        (neg.mean() + 3 * neg.std(ddof=1)).where(neg.count() >= 2).reindex(analytes)
    )

    cutoff_row = df["analyte"].map(cutoff_series)
    df["call"] = np.where(
        ~df["valid"] | cutoff_row.isna() | df["net_mfi"].isna(),
        "INVALID",
        np.where(df["net_mfi"] > cutoff_row, "POS", "NEG"),
    )

    pos = df[(df["sample_type"] == "pos_control") & df["valid"]]
    qc = pos.groupby("analyte")["call"].agg(n="size", n_pos=lambda s: (s == "POS").sum())
    ok = ((qc["n"] >= 1) & (qc["n"] == qc["n_pos"])).reindex(analytes, fill_value=False)
    run_valid = bool(ok.all())

    cutoffs = {a: (None if pd.isna(v) else float(v)) for a, v in cutoff_series.items()}
    return df, cutoffs, run_valid
