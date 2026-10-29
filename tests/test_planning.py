"""Planning: what a profile would do, and how that is explained."""

from __future__ import annotations

import unittest

from tests import TempDirTestCase  # noqa: F401

from quietgovernor.backend import SimulatedBackend, capture_baseline, read_all
from quietgovernor.errors import BackendError
from quietgovernor.planning import Baseline, Reading, build_plan, plan_for_writes
from quietgovernor.policy import (
    AC,
    BOOST_UNAVAILABLE_NOTE,
    BUILTIN_PROFILES,
    DC,
    build_profile,
    get_builtin,
)


def readings_from(backend: SimulatedBackend):
    return read_all(backend)


class TestBasics(unittest.TestCase):
    def test_quiet_profile_changes_six_values_on_a_default_pc(self) -> None:
        plan = build_plan(get_builtin("quiet"), readings_from(SimulatedBackend()))
        self.assertEqual(len(plan.changes), 6)
        self.assertFalse(plan.is_empty)
        self.assertIn("would change 6 settings", plan.summary())
        self.assertIn("Balanced", plan_for_writes(
            get_builtin("quiet"), "guid", "Balanced", readings_from(SimulatedBackend())
        ).summary())

    def test_changes_are_ordered_boost_then_max_then_min(self) -> None:
        plan = build_plan(get_builtin("quiet"), readings_from(SimulatedBackend()))
        keys = [change.setting_key for change in plan.changes]
        self.assertEqual(keys[:2], ["boost_mode", "boost_mode"])
        self.assertEqual(keys[2:4], ["max_state", "max_state"])
        self.assertEqual(keys[4:], ["min_state", "min_state"])

    def test_already_matching_machine_produces_an_empty_plan(self) -> None:
        matching = SimulatedBackend(
            {"boost_mode": {AC: 0, DC: 0}, "max_state": {AC: 60, DC: 60}, "min_state": {AC: 0, DC: 0}}
        )
        plan = build_plan(get_builtin("quiet"), readings_from(matching))
        self.assertTrue(plan.is_empty)
        self.assertIn("Nothing to change", plan.summary())
        self.assertEqual(len(plan.unchanged), 6)

    def test_leaving_a_setting_alone_means_it_is_not_written(self) -> None:
        # 'custom' asks for full speed, so it only differs from a default PC in
        # the minimum state; it must not touch the maximum or boost settings.
        plan = build_plan(get_builtin("custom"), readings_from(SimulatedBackend()))
        self.assertEqual({change.setting_key for change in plan.changes}, {"min_state"})
        self.assertFalse(any("turbo" in note for note in plan.notes))


class TestExplanations(unittest.TestCase):
    def test_each_change_reads_as_a_sentence(self) -> None:
        plan = build_plan(get_builtin("quiet"), readings_from(SimulatedBackend()))
        text = " ".join(change.describe() for change in plan.changes)
        self.assertIn("Maximum processor state on plugged-in power is lowered from 100% to 60%", text)
        self.assertIn("Minimum processor state on battery power is lowered from 5% to 0%", text)

    def test_sub_one_hundred_caps_explain_turbo(self) -> None:
        plan = build_plan(get_builtin("quiet"), readings_from(SimulatedBackend()))
        self.assertTrue(any("turbo" in note for note in plan.notes))

    def test_uncapped_profile_does_not_mention_turbo(self) -> None:
        profile = build_profile("plain", "", {"boost_mode": {AC: 0, DC: 0}})
        plan = build_plan(profile, readings_from(SimulatedBackend()))
        self.assertFalse(any("turbo" in note for note in plan.notes))

    def test_low_cap_produces_a_warning(self) -> None:
        profile = build_profile("very-low", "", {"max_state": {AC: 25, DC: 25}})
        plan = build_plan(profile, readings_from(SimulatedBackend()))
        warnings = [check for check in plan.checks if not check.passed]
        self.assertEqual(len(warnings), 2)
        self.assertIn("Consider 40%", warnings[0].detail)
        rendered = "\n".join(plan.detail_lines())
        self.assertIn("Things to know", rendered)

    def test_a_normal_cap_passes_the_check(self) -> None:
        plan = build_plan(get_builtin("quiet"), readings_from(SimulatedBackend()))
        self.assertTrue(all(check.passed for check in plan.checks))

    def test_detail_lines_include_warnings_and_notes(self) -> None:
        plan = build_plan(
            build_profile("low", "", {"max_state": {AC: 20, DC: 20}}),
            readings_from(SimulatedBackend()),
        )
        rendered = "\n".join(plan.detail_lines())
        self.assertIn("Things to know before you decide:", rendered)
        self.assertIn("Note:", rendered)


class TestUnavailableSettings(unittest.TestCase):
    def test_missing_boost_mode_is_skipped_with_one_note(self) -> None:
        backend = SimulatedBackend(unavailable={"boost_mode"})
        plan = build_plan(get_builtin("quiet"), readings_from(backend))
        self.assertEqual({change.setting_key for change in plan.changes}, {"max_state", "min_state"})
        self.assertEqual(plan.notes.count(BOOST_UNAVAILABLE_NOTE), 1)

    def test_missing_max_state_is_an_error_that_explains_itself(self) -> None:
        backend = SimulatedBackend(unavailable={"max_state"})
        with self.assertRaises(BackendError) as caught:
            build_plan(get_builtin("quiet"), readings_from(backend))
        message = str(caught.exception)
        self.assertIn("Maximum processor state", message)
        self.assertIn("No settings were changed", message)


class TestBaseline(unittest.TestCase):
    def test_capture_records_scheme_and_values(self) -> None:
        backend = SimulatedBackend({"max_state": {AC: 88, DC: 77}})
        baseline = capture_baseline(backend)
        self.assertEqual(baseline.scheme_name, "Balanced")
        self.assertEqual(baseline.readings["max_state"].value_for(AC), 88)
        self.assertEqual(baseline.readings["max_state"].value_for(DC), 77)
        self.assertTrue(baseline.captured_at)

    def test_round_trip(self) -> None:
        baseline = capture_baseline(SimulatedBackend())
        restored = Baseline.from_dict(baseline.to_dict())
        self.assertEqual(restored.scheme_guid, baseline.scheme_guid)
        self.assertEqual(
            restored.readings["max_state"].value_for(AC),
            baseline.readings["max_state"].value_for(AC),
        )

    def test_future_schema_version_is_refused(self) -> None:
        payload = capture_baseline(SimulatedBackend()).to_dict()
        payload["schema_version"] = 99
        with self.assertRaises(ValueError) as caught:
            Baseline.from_dict(payload)
        self.assertIn("unsupported baseline schema version", str(caught.exception))

    def test_unknown_setting_is_refused(self) -> None:
        payload = capture_baseline(SimulatedBackend()).to_dict()
        payload["readings"]["fan_curve"] = {"available": True, "values": {}}
        with self.assertRaises(ValueError):
            Baseline.from_dict(payload)

    def test_unavailable_readings_survive_a_round_trip(self) -> None:
        baseline = capture_baseline(SimulatedBackend(unavailable={"boost_mode"}))
        restored = Baseline.from_dict(baseline.to_dict())
        self.assertFalse(restored.readings["boost_mode"].available)
        self.assertIsNone(restored.readings["boost_mode"].value_for(AC))


class TestReading(unittest.TestCase):
    def test_reading_validation(self) -> None:
        self.assertEqual(Reading.from_dict({"available": True, "values": {AC: 5}}).value_for(AC), 5)
        for bad in (
            {"available": "yes", "values": {}},
            {"available": True, "values": {"USB": 5}},
            {"available": True, "values": {AC: "5"}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Reading.from_dict(bad)

    def test_unavailable_reading_yields_nothing(self) -> None:
        reading = Reading(available=False, values={AC: 50})
        self.assertIsNone(reading.value_for(AC))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
