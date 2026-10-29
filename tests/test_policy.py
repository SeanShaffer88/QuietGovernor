"""Policy: identifiers, ranges, consistency, and the built-in profiles."""

from __future__ import annotations

import unittest

from tests import TempDirTestCase  # noqa: F401  (puts src/ on sys.path)

from quietgovernor.errors import PolicyError
from quietgovernor.policy import (
    AC,
    BUILTIN_PROFILES,
    DC,
    SETTINGS,
    Profile,
    build_profile,
    check_consistency,
    get_builtin,
    is_guid,
    validate_scope,
    validate_value,
)


class TestSettingIdentifiers(unittest.TestCase):
    """These GUIDs are the whole product; a typo would change nothing at all."""

    def test_subgroup_and_settings_are_pinned(self) -> None:
        self.assertEqual(SETTINGS["min_state"].subgroup, "54533251-82be-4824-96c1-47b60b740d00")
        self.assertEqual(SETTINGS["min_state"].guid, "893dee8e-2bef-41e0-89c6-b55d0929964c")
        self.assertEqual(SETTINGS["max_state"].guid, "bc5038f7-23e0-4960-96da-33abaf5935ec")
        self.assertEqual(SETTINGS["boost_mode"].guid, "be337238-0d82-4146-a960-4f3749d470c7")

    def test_every_identifier_is_a_guid(self) -> None:
        for setting in SETTINGS.values():
            self.assertTrue(is_guid(setting.guid), setting.key)
            self.assertTrue(is_guid(setting.subgroup), setting.key)

    def test_is_guid_rejects_near_misses(self) -> None:
        self.assertFalse(is_guid("54533251-82be-4824-96c1-47b60b740d0"))  # short
        self.assertFalse(is_guid("PROCTHROTTLEMIN"))
        self.assertFalse(is_guid(""))


class TestValueValidation(unittest.TestCase):
    def test_boundaries_are_accepted(self) -> None:
        self.assertEqual(validate_value("max_state", 20), 20)
        self.assertEqual(validate_value("max_state", 100), 100)
        self.assertEqual(validate_value("min_state", 0), 0)
        self.assertEqual(validate_value("boost_mode", 5), 5)

    def test_out_of_range_is_rejected_with_a_fix_path(self) -> None:
        with self.assertRaises(PolicyError) as caught:
            validate_value("max_state", 19)
        message = str(caught.exception)
        self.assertIn("between 20 and 100", message)
        self.assertIn("Pick a value inside that range", message)

    def test_min_state_above_50_is_rejected(self) -> None:
        # A high minimum keeps clocks up: louder, and the opposite of the
        # product's purpose, so it is not offered.
        with self.assertRaises(PolicyError):
            validate_value("min_state", 51)

    def test_non_integers_are_rejected(self) -> None:
        for value in ("60", 60.0, None, True):
            with self.assertRaises(PolicyError):
                validate_value("max_state", value)

    def test_unknown_setting_is_rejected(self) -> None:
        with self.assertRaises(PolicyError) as caught:
            validate_value("fan_speed", 3)
        self.assertIn("unknown setting", str(caught.exception))

    def test_describe_explains_known_and_unknown_values(self) -> None:
        self.assertIn("idle", SETTINGS["min_state"].describe(0))
        self.assertIn("capped at 60%", SETTINGS["max_state"].describe(60))
        self.assertEqual(SETTINGS["max_state"].describe(45), "45% of full processor speed")
        self.assertEqual(SETTINGS["boost_mode"].describe(0), "boost is off — the processor stays at or below its rated speed")


class TestConsistency(unittest.TestCase):
    def test_minimum_above_maximum_is_rejected(self) -> None:
        with self.assertRaises(PolicyError) as caught:
            check_consistency({"min_state": {AC: 80, DC: 80}, "max_state": {AC: 60, DC: 60}})
        self.assertIn("higher than maximum", str(caught.exception))

    def test_engine_equal_min_and_max_is_allowed(self) -> None:
        check_consistency({"min_state": {AC: 60, DC: 60}, "max_state": {AC: 60, DC: 60}})

    def test_missing_source_is_ignored(self) -> None:
        check_consistency({"min_state": {AC: 80}, "max_state": {DC: 10}})


class TestScope(unittest.TestCase):
    def test_scopes(self) -> None:
        self.assertEqual(validate_scope("both"), "both")
        self.assertEqual(validate_scope("ac"), "ac")
        with self.assertRaises(PolicyError) as caught:
            validate_scope("mains")
        self.assertIn("scope must be one of", str(caught.exception))


class TestBuildProfile(unittest.TestCase):
    def test_values_outside_scope_are_rejected(self) -> None:
        with self.assertRaises(PolicyError) as caught:
            build_profile("x", "", {"max_state": {AC: 60, DC: 60}}, scope="ac")
        self.assertIn("only covers AC", str(caught.exception))

    def test_partial_profile_keeps_only_named_values(self) -> None:
        profile = build_profile("x", "", {"max_state": {AC: 60, DC: 60}}, partial=True)
        self.assertTrue(profile.partial)
        self.assertIsNone(profile.value_for("min_state", AC))
        self.assertEqual(profile.resolved_values(), [("max_state", AC, 60), ("max_state", DC, 60)])

    def test_unknown_power_source_is_rejected(self) -> None:
        with self.assertRaises(PolicyError):
            build_profile("x", "", {"max_state": {"USB": 60}})


class TestProfileSerialisation(unittest.TestCase):
    def test_round_trip(self) -> None:
        original = build_profile(
            "mine", "test", {"max_state": {AC: 70, DC: 65}, "boost_mode": {AC: 0, DC: 0}}
        )
        restored = Profile.from_dict(original.to_dict())
        self.assertEqual(restored.name, "mine")
        self.assertEqual(restored.values["max_state"][DC], 65)
        self.assertFalse(restored.partial)

    def test_from_dict_rejects_bad_data(self) -> None:
        cases = [
            {},
            {"name": "", "values": {}},
            {"name": "x", "values": {"nope": {AC: 1}}},
            {"name": "x", "values": {"max_state": {"USB": 50}}},
            {"name": "x", "values": {"max_state": {AC: 500}}},
            {"name": "x", "values": {"max_state": {AC: "60"}}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                Profile.from_dict(case)


class TestBuiltinProfiles(unittest.TestCase):
    def test_expected_profiles_exist(self) -> None:
        self.assertEqual(set(BUILTIN_PROFILES), {"quiet", "balanced", "custom", "restore"})

    def test_profiles_are_valid_and_quiet_is_actually_quiet(self) -> None:
        for name, profile in BUILTIN_PROFILES.items():
            with self.subTest(profile=name):
                # Rebuilding through build_profile re-validates every value.
                if not profile.partial:
                    build_profile(
                        profile.name,
                        profile.description,
                        profile.values,
                        scope=profile.scope,
                    )
        quiet = BUILTIN_PROFILES["quiet"]
        self.assertLess(quiet.value_for("max_state", AC) or 100, 100)
        self.assertEqual(quiet.value_for("boost_mode", AC), 0)
        balanced = BUILTIN_PROFILES["balanced"]
        self.assertLess(balanced.value_for("max_state", AC) or 100, 100)

    def test_restore_is_partial_and_empty(self) -> None:
        restore = BUILTIN_PROFILES["restore"]
        self.assertTrue(restore.partial)
        self.assertEqual(restore.resolved_values(), [])

    def test_get_builtin_reports_alternatives(self) -> None:
        with self.assertRaises(PolicyError) as caught:
            get_builtin("silent")
        self.assertIn("quiet", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
