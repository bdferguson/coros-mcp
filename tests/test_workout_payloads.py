"""Tests for the strength-workout payload builder.

These pin the reverse-engineered encoding rules in
`_build_strength_program_payload` so they survive future tweaks.
Pure JSON-shape assertions — no HTTP, no auth, no mocks.
"""

import pytest

from coros_api import (
    _build_strength_program_payload,
    _build_workout_program_payload,
)


def _exercise(**overrides):
    """Minimal exercise dict — only the keys the builder reads."""
    base = {
        "origin_id": "0",
        "name": "T0000",
        "overview": "sid_strength_test",
        "target_type": 3,
        "target_value": 10,
        "rest_seconds": 60,
    }
    base.update(overrides)
    return base


def _build(exercises=None, by_id=None, sets=1):
    if exercises is None:
        exercises = [_exercise()]
    if by_id is None:
        by_id = {}
    return _build_strength_program_payload(
        name="test workout",
        exercises=exercises,
        by_id=by_id,
        sets=sets,
    )


# ---------------------------------------------------------------------------
# Weight encoding — bodyweight / kg / lbs
# ---------------------------------------------------------------------------

def test_bodyweight_omits_both():
    payload = _build([_exercise()])
    ex = payload["exercises"][0]
    assert ex["intensityValue"] == ""
    assert ex["intensityCustom"] == 1
    assert ex["intensityDisplayUnit"] == "6"


def test_weight_kg():
    payload = _build([_exercise(weight_kg=27.9)])
    ex = payload["exercises"][0]
    assert ex["intensityValue"] == 27900
    assert ex["intensityPercent"] == 0
    assert ex["intensityDisplayUnit"] == "6"
    assert ex["intensityCustom"] == 0
    assert ex["isIntensityPercent"] is False


def test_weight_kg_zero_renders_zero_kg():
    """weight_kg=0 explicitly is NOT bodyweight."""
    payload = _build([_exercise(weight_kg=0)])
    ex = payload["exercises"][0]
    assert ex["intensityValue"] == 0
    assert ex["intensityCustom"] == 0
    assert ex["intensityDisplayUnit"] == "6"


def test_weight_lbs():
    payload = _build([_exercise(weight_lbs=45)])
    ex = payload["exercises"][0]
    # 45 * 0.45359237 * 1000 = 20411.65665 → 20412
    assert ex["intensityValue"] == 20412
    assert ex["intensityPercent"] == 45_000_000
    assert ex["intensityDisplayUnit"] == "7"
    assert ex["intensityCustom"] == 0


def test_weight_kg_and_lbs_raises():
    with pytest.raises(ValueError):
        _build([_exercise(weight_kg=10, weight_lbs=22)])


def test_negative_weight_kg_raises():
    with pytest.raises(ValueError):
        _build([_exercise(weight_kg=-1)])


def test_negative_weight_lbs_raises():
    with pytest.raises(ValueError):
        _build([_exercise(weight_lbs=-1)])


# ---------------------------------------------------------------------------
# Rest encoding — Skip rests vs MM:SS
# ---------------------------------------------------------------------------

def test_skip_rests_when_zero():
    payload = _build([_exercise(rest_seconds=0)])
    ex = payload["exercises"][0]
    assert ex["restType"] == 3
    assert ex["restValue"] == 0


def test_rest_seconds_positive():
    payload = _build([_exercise(rest_seconds=90)])
    ex = payload["exercises"][0]
    assert ex["restType"] == 1
    assert ex["restValue"] == 90


# ---------------------------------------------------------------------------
# Per-exercise sets vs circuit sets
# ---------------------------------------------------------------------------

def test_per_exercise_sets():
    payload = _build([_exercise(sets=3)], sets=1)
    assert payload["exercises"][0]["sets"] == 3


# ---------------------------------------------------------------------------
# Regression-pinned constants (commit cf2cec4, payload contract)
# ---------------------------------------------------------------------------

def test_status_one_on_every_exercise():
    """Restored 2026-05-21 (commit cf2cec4) — API may treat missing as
    disabled in the future."""
    payload = _build([
        _exercise(name="A"),
        _exercise(name="B", weight_kg=10),
        _exercise(name="C", weight_lbs=20),
    ])
    for ex in payload["exercises"]:
        assert ex["status"] == 1


def test_sport_type_4_program_and_exercise():
    payload = _build([_exercise(), _exercise()])
    assert payload["sportType"] == 4
    for ex in payload["exercises"]:
        assert ex["sportType"] == 4


def test_exercise_num_and_total_sets():
    payload = _build([_exercise(), _exercise(), _exercise()], sets=2)
    assert payload["exerciseNum"] == 3
    assert payload["totalSets"] == 2
    assert payload["sets"] == 2


def test_intensity_type_one_for_strength():
    payload = _build([_exercise(), _exercise(weight_kg=10)])
    for ex in payload["exercises"]:
        assert ex["intensityType"] == 1


# ---------------------------------------------------------------------------
# Duration math
# ---------------------------------------------------------------------------

def test_duration_per_exercise_sets():
    """1 exercise, time target 30s + 10s rest, per-ex sets=3, circuit sets=1."""
    payload = _build(
        [_exercise(target_type=2, target_value=30, rest_seconds=10, sets=3)],
        sets=1,
    )
    assert payload["duration"] == (30 + 10) * 3


def test_duration_circuit_sets():
    """1 exercise, time target 30s + 10s rest, per-ex sets=1, circuit sets=3."""
    payload = _build(
        [_exercise(target_type=2, target_value=30, rest_seconds=10)],
        sets=3,
    )
    assert payload["duration"] == (30 + 10) * 1 * 3


def test_duration_reps_target_excludes_value():
    """For target_type=3 (reps), only rest counts toward duration."""
    payload = _build(
        [_exercise(target_type=3, target_value=12, rest_seconds=60)],
        sets=1,
    )
    assert payload["duration"] == 60


# ---------------------------------------------------------------------------
# Catalog enrichment (Training Machines / Training Parts diagrams)
# ---------------------------------------------------------------------------

def test_catalog_metadata_propagates_when_present():
    by_id = {
        "T1061": {
            "id": "T1061",
            "muscle": ["quads", "glutes"],
            "muscleRelevance": [1.0, 0.8],
            "part": ["legs"],
            "equipment": [3],
        }
    }
    payload = _build([_exercise(origin_id="T1061")], by_id=by_id)
    ex = payload["exercises"][0]
    assert ex["muscle"] == ["quads", "glutes"]
    assert ex["muscleRelevance"] == [1.0, 0.8]
    assert ex["part"] == ["legs"]
    assert ex["equipment"] == [3]


def test_catalog_miss_gives_empty_lists():
    """Resilience per commit b1c8328 — workout still creates, only
    diagram metadata is lost."""
    payload = _build([_exercise(origin_id="T9999")], by_id={})
    ex = payload["exercises"][0]
    assert ex["muscle"] == []
    assert ex["muscleRelevance"] == []
    assert ex["part"] == []
    assert ex["equipment"] == []


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

def test_empty_exercises_raises():
    with pytest.raises(ValueError):
        _build(exercises=[])


# ---------------------------------------------------------------------------
# Cycling/intervals builder (_build_workout_program_payload)
# ---------------------------------------------------------------------------

def test_cycling_plain_steps_total_seconds():
    payload = _build_workout_program_payload(
        name="Z2",
        steps=[
            {"name": "Warmup", "duration_minutes": 10, "intensity_low": 150, "intensity_high": 200},
            {"name": "Main",   "duration_minutes": 30, "intensity_low": 200, "intensity_high": 240},
        ],
    )
    assert payload["estimatedTime"] == (10 + 30) * 60
    assert payload["name"] == "Z2"
    assert payload["sportType"] == 2
    assert payload["access"] == 1
    assert len(payload["exercises"]) == 2


def test_cycling_repeat_group_expands_total():
    """Repeat group: iteration_seconds * repeat is added to estimatedTime,
    and the group header + sub-steps are all emitted (1 header + N subs)."""
    payload = _build_workout_program_payload(
        name="3x10",
        steps=[
            {"name": "Warmup", "duration_minutes": 10, "intensity_low": 150, "intensity_high": 200},
            {"repeat": 3, "steps": [
                {"name": "On",  "duration_minutes": 10, "intensity_low": 265, "intensity_high": 285},
                {"name": "Off", "duration_minutes": 3,  "intensity_low": 150, "intensity_high": 175},
            ]},
        ],
    )
    # 10 + 3*(10+3) = 49 min
    assert payload["estimatedTime"] == (10 + 3 * (10 + 3)) * 60
    # 1 warmup + 1 group header + 2 sub-steps = 4 exercises
    assert len(payload["exercises"]) == 4


def test_cycling_repeat_group_links_subs_to_header():
    """Sub-steps reference the group header via groupId; header has isGroup=True."""
    payload = _build_workout_program_payload(
        name="2x5",
        steps=[
            {"repeat": 2, "steps": [
                {"name": "On",  "duration_minutes": 5, "intensity_low": 200, "intensity_high": 230},
                {"name": "Off", "duration_minutes": 2, "intensity_low": 150, "intensity_high": 175},
            ]},
        ],
    )
    header, sub1, sub2 = payload["exercises"]
    assert header["isGroup"] is True
    assert header["sets"] == 2
    assert sub1["isGroup"] is False
    assert sub1["groupId"] == str(header["id"])
    assert sub2["groupId"] == str(header["id"])


def test_cycling_power_legacy_aliases():
    """power_low_w / power_high_w are accepted as legacy aliases."""
    payload = _build_workout_program_payload(
        name="legacy",
        steps=[
            {"name": "Step", "duration_minutes": 5, "power_low_w": 200, "power_high_w": 240},
        ],
    )
    ex = payload["exercises"][0]
    assert ex["intensityValue"] == 200
    assert ex["intensityValueExtend"] == 240


def test_cycling_sport_and_intensity_types_propagate():
    payload = _build_workout_program_payload(
        name="hr",
        steps=[{"name": "S", "duration_minutes": 5, "intensity_low": 140, "intensity_high": 160}],
        sport_type=200,
        intensity_type=2,
    )
    assert payload["sportType"] == 200
    for ex in payload["exercises"]:
        assert ex["sportType"] == 200
    # Non-group steps use the caller-provided intensity_type
    assert payload["exercises"][0]["intensityType"] == 2
