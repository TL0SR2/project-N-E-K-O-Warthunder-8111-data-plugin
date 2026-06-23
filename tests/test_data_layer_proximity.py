"""Regression tests for vendored data-layer proximity helpers."""

from __future__ import annotations

import sys
from pathlib import Path


DATA_PROCESS = Path(__file__).resolve().parents[1] / "data_layer" / "data process"
if str(DATA_PROCESS) not in sys.path:
    sys.path.insert(0, str(DATA_PROCESS))


def test_proximity_thresholds_use_profile_without_type_error():
    from wt_proximity import resolve_proximity_thresholds

    profiles = {
        "_default": {"proximity_warn_m": 3000},
        "f-4f_kws_lv": {"proximity_warn_m": 5000},
    }

    assert resolve_proximity_thresholds(profiles, "air", "f-4f_kws_lv") == (
        5000,
        None,
    )
