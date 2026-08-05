"""The vendored per-car drivetrain table.

This table decides which shaker a gear shift and the engine rumble land on, so
the failure mode that matters is not "missing car" — that falls back safely —
but "wrong answer stated confidently". These tests pin the shape of the data
and the two derived properties the audio path will actually consume.
"""

from __future__ import annotations

from shaker.gt7 import drivetrain

# Spot checks against cars whose layout is not in dispute. Codes come from the
# GT7 car list; a permuted or corrupt table shows up here immediately.
_KNOWN = {
    24: ("FR", "rear", "front"),    # Nissan 180SX Type X '96
    31: ("FR", "rear", "front"),    # Chevrolet Camaro Z28 '69
    3246: ("4WD", "both", None),    # Bugatti Veyron Gr.4
}

_VALID_LAYOUTS = {"FF", "FR", "MR", "RR", "4WD"}


def test_known_cars_resolve_correctly() -> None:
    for code, (layout, axle, engine) in _KNOWN.items():
        assert drivetrain.layout_for(code) == layout, code
        assert drivetrain.driven_axle(code) == axle, code
        assert drivetrain.engine_position(code) == engine, code


def test_table_is_populated_and_well_formed() -> None:
    assert len(drivetrain._LAYOUT) > 500
    assert set(drivetrain._LAYOUT.values()) == _VALID_LAYOUTS
    assert all(isinstance(code, int) for code in drivetrain._LAYOUT)


def test_every_layout_has_a_driven_axle() -> None:
    """A layout with no driven axle would silently disable shift routing."""
    for layout in _VALID_LAYOUTS:
        assert drivetrain._DRIVEN_AXLE[layout] in {"front", "rear", "both"}


def test_unknown_car_is_none_everywhere() -> None:
    """Unknown must mean "no opinion", never a guess — the caller falls back
    to its configured defaults."""
    for code in (0, -1, 999999):
        assert drivetrain.layout_for(code) is None
        assert drivetrain.driven_axle(code) is None
        assert drivetrain.engine_position(code) is None


def test_none_car_code_is_handled() -> None:
    """car_code is absent until a packet has been parsed."""
    assert drivetrain.layout_for(None) is None
    assert drivetrain.driven_axle(None) is None
    assert drivetrain.engine_position(None) is None


def test_front_drive_cars_take_shift_shock_at_the_front() -> None:
    """The whole point of the table: an FF car's shift must not thump the
    seat through an axle that isn't driven."""
    assert drivetrain._DRIVEN_AXLE["FF"] == "front"
    assert drivetrain._DRIVEN_AXLE["FR"] == "rear"


def test_engine_position_is_absent_for_four_wheel_drive() -> None:
    """The source records drive type, not engine position, and 4WD spans both
    extremes. None here is deliberate — guessing would put a mid-engine car's
    thrum under the pedals."""
    assert drivetrain._ENGINE_POSITION.get("4WD") is None
    assert drivetrain._ENGINE_POSITION["MR"] == "rear"
    assert drivetrain._ENGINE_POSITION["RR"] == "rear"
    assert drivetrain._ENGINE_POSITION["FF"] == "front"


def test_mid_and_rear_engine_cars_are_well_represented() -> None:
    """These are the layouts today's centred default is most wrong for, so a
    table that lost them would quietly do nothing useful."""
    counts: dict[str, int] = {}
    for layout in drivetrain._LAYOUT.values():
        counts[layout] = counts.get(layout, 0) + 1
    assert counts["MR"] > 100
    assert counts["RR"] > 15
    assert counts["FF"] > 40
