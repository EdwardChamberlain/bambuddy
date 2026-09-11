"""Tests for sparse SliceModal process-setting overrides."""

import json

from backend.app.services.process_overrides import apply_process_overrides, normalise_process_overrides


def test_normalise_process_overrides_uses_slicer_scalar_forms():
    result = normalise_process_overrides(
        {
            "layer_height": 0.2,
            "enable_support": True,
            "sparse_infill_density": "15%",
            "wall_loops": [2, 3],
        }
    )
    assert result == {
        "layer_height": "0.2",
        "enable_support": "1",
        "sparse_infill_density": "15%",
        "wall_loops": ["2", "3"],
    }


def test_normalise_process_overrides_drops_malformed_entries():
    result = normalise_process_overrides(
        {
            "Layer Height": 0.2,
            "layer_height": {"unexpected": "object"},
            "wall_loops": 3,
        }
    )
    assert result == {"wall_loops": "3"}


def test_apply_process_overrides_updates_only_requested_keys():
    source = json.dumps({"layer_height": "0.12", "wall_loops": "2", "brim_type": "auto"})
    result = json.loads(
        apply_process_overrides(
            source,
            {"layer_height": "0.2", "enable_support": False},
        )
    )
    assert result == {
        "layer_height": "0.2",
        "wall_loops": "2",
        "brim_type": "auto",
        "enable_support": "0",
    }


def test_apply_process_overrides_leaves_invalid_profile_unchanged():
    source = "not-json"
    assert apply_process_overrides(source, {"wall_loops": 3}) == source
