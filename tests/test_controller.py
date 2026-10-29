"""Controller: baseline-before-write, confirmation, verification, rollback,
restore, drift, and user profiles."""

from __future__ import annotations

import unittest

from tests import TempDirTestCase

from quietgovernor.backend import SimulatedBackend
from quietgovernor.controller import Controller
from quietgovernor.errors import (
    ActionRefused,
    ApplyFailed,
    BackendError,
    NotElevatedError,
    PlanNotActive,
    QuietGovernorError,
)
from quietgovernor.policy import AC, DC
from quietgovernor.store import StateStore

ORIGINAL = {"min_state": 5, "max_state": 100, "boost_mode": 1}


class QuotaBackend(SimulatedBackend):
    """A simulated PC whose power service stops accepting writes."""

    def __init__(self, allow_writes: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._budget = allow_writes

    def write(self, setting_key: str, source: str, value: int) -> None:
        if self._budget <= 0:
            raise BackendError("The simulated power service refused the change.")
        self._budget -= 1
        super().write(setting_key, source, value)


class UnelevatedBackend(SimulatedBackend):
    def ensure_writable(self) -> None:
        raise NotElevatedError("Changing power settings needs administrator rights.")


class ControllerTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.backend = SimulatedBackend()
        self.store = StateStore(self.state_dir)
        self.controller = Controller(self.backend, self.store)

    def values(self) -> dict[str, dict[str, int]]:
        return self.backend.effective_values()


class TestApply(ControllerTestCase):
    def test_apply_writes_values_records_state_and_baseline(self) -> None:
        result = self.controller.apply("quiet", confirm=lambda plan: True)
        self.assertTrue(result.changed)
        self.assertTrue(result.verified)
        self.assertTrue(result.baseline_created)
        values = self.values()
        self.assertEqual(values["max_state"], {AC: 60, DC: 60})
        self.assertEqual(values["boost_mode"], {AC: 0, DC: 0})
        state = self.store.load_state()
        assert state is not None
        self.assertEqual(state.profile_name, "quiet")
        self.assertEqual(state.managed_value("max_state", DC), 60)
        self.assertIsNotNone(self.store.load_baseline())

    def test_baseline_is_captured_before_the_first_write(self) -> None:
        self.backend.write  # silence linters about attribute use below
        self.controller.apply("quiet", confirm=lambda plan: True)
        baseline = self.store.load_baseline()
        assert baseline is not None
        for key, expected in ORIGINAL.items():
            self.assertEqual(baseline.readings[key].value_for(AC), expected, key)
            self.assertEqual(baseline.readings[key].value_for(DC), expected, key)

    def test_declining_confirmation_changes_nothing(self) -> None:
        with self.assertRaises(ActionRefused) as caught:
            self.controller.apply("quiet", confirm=lambda plan: False)
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(self.values()["max_state"], {AC: 100, DC: 100})
        self.assertEqual(self.backend.writes, [])
        self.assertIsNone(self.store.load_baseline())
        self.assertIsNone(self.store.load_state())

    def test_preview_writes_nothing_and_records_no_baseline(self) -> None:
        result = self.controller.apply("quiet", dry_run=True, confirm=lambda plan: True)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.changed)
        self.assertEqual(self.backend.writes, [])
        self.assertIsNone(self.store.load_baseline())
        self.assertTrue(any("preview makes no changes" in note for note in result.plan.notes))

    def test_second_apply_keeps_the_original_baseline(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        first = self.store.load_baseline()
        assert first is not None
        self.controller.apply("balanced", confirm=lambda plan: True)
        second = self.store.load_baseline()
        assert second is not None
        self.assertEqual(
            first.readings["max_state"].value_for(AC),
            second.readings["max_state"].value_for(AC),
        )
        self.assertEqual(first.captured_at, second.captured_at)

    def test_applying_when_already_matching_claims_nothing(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        result = self.controller.apply("quiet", confirm=lambda plan: True)
        self.assertFalse(result.changed)
        self.assertTrue(result.plan.is_empty)
        self.assertTrue(self.store.load_state().is_governing)  # type: ignore[union-attr]

    def test_apply_is_idempotent_in_effect(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        before = self.values()
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.assertEqual(before, self.values())

    def test_unavailable_boost_mode_is_skipped_not_fatal(self) -> None:
        backend = SimulatedBackend(unavailable={"boost_mode"})
        controller = Controller(backend, StateStore(self.state_dir))
        result = controller.apply("quiet", confirm=lambda plan: True)
        self.assertEqual({change.setting_key for change in result.applied}, {"max_state", "min_state"})
        self.assertTrue(any("boost" in note.lower() for note in result.plan.notes))

    def test_scope_limits_writes(self) -> None:
        result = self.controller.apply("quiet", scope="ac", confirm=lambda plan: True)
        self.assertTrue(all(change.source == AC for change in result.applied))
        self.assertEqual(self.values()["max_state"], {AC: 60, DC: 100})

    def test_unelevated_backend_fails_before_writing(self) -> None:
        backend = UnelevatedBackend()
        controller = Controller(backend, StateStore(self.state_dir))
        with self.assertRaises(NotElevatedError):
            controller.apply("quiet", confirm=lambda plan: True)
        self.assertEqual(backend.writes, [])


class TestFailureHandling(ControllerTestCase):
    def test_a_failure_partway_rolls_everything_back(self) -> None:
        backend = SimulatedBackend(fail_on=("min_state", DC))
        controller = Controller(backend, StateStore(self.state_dir))
        with self.assertRaises(ApplyFailed) as caught:
            controller.apply("quiet", confirm=lambda plan: True)
        self.assertTrue(caught.exception.rolled_back)
        self.assertIn("put back to their original values", str(caught.exception))
        values = backend.effective_values()
        for key, expected in ORIGINAL.items():
            self.assertEqual(values[key], {AC: expected, DC: expected}, key)
        self.assertIsNone(self.store.load_state())

    def test_settings_changing_during_the_question_abort_the_apply(self) -> None:
        backend = self.backend

        def meddling_confirm(plan) -> bool:
            # Stand-in for the Windows user changing the plan, or another tool
            # writing a value, while the confirmation prompt is on screen.
            backend.write("max_state", AC, 80)
            return True

        with self.assertRaises(BackendError) as caught:
            self.controller.apply("quiet", confirm=meddling_confirm)
        self.assertIn("changed while this plan was waiting", str(caught.exception))
        self.assertEqual(self.values()["max_state"], {AC: 80, DC: 100})
        self.assertIsNone(self.store.load_baseline(), "no baseline from a refuted plan")

    def test_a_baseline_survives_a_failed_first_apply(self) -> None:
        backend = SimulatedBackend(fail_on=("min_state", DC))
        controller = Controller(backend, StateStore(self.state_dir))
        with self.assertRaises(ApplyFailed):
            controller.apply("quiet", confirm=lambda plan: True)
        baseline = self.store.load_baseline()
        self.assertIsNotNone(baseline, "the restore point must not be lost on failure")

    def test_failed_rollback_tells_the_user_how_to_recover(self) -> None:
        backend = QuotaBackend(allow_writes=2)
        controller = Controller(backend, StateStore(self.state_dir))
        with self.assertRaises(ApplyFailed) as caught:
            controller.apply("quiet", confirm=lambda plan: True)
        self.assertFalse(caught.exception.rolled_back)
        message = str(caught.exception)
        self.assertIn("quietgovernor restore", message)
        self.assertIn("baseline.json", message)

    def test_verification_catches_a_write_windows_did_not_apply(self) -> None:
        class IgnoringBackend(SimulatedBackend):
            def write(self, setting_key: str, source: str, value: int) -> None:
                if setting_key == "min_state":
                    self.writes.append((setting_key, source, value))
                    return  # claims success, changes nothing
                super().write(setting_key, source, value)

        controller = Controller(IgnoringBackend(), StateStore(self.state_dir))
        with self.assertRaises(ApplyFailed) as caught:
            controller.apply("quiet", confirm=lambda plan: True)
        self.assertTrue(caught.exception.rolled_back)
        self.assertIn("reported", str(caught.exception))


class TestRestore(ControllerTestCase):
    def test_restore_puts_the_original_values_back_and_stops_governing(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        result = self.controller.restore(confirm=lambda plan: True)
        self.assertTrue(result.changed)
        self.assertTrue(result.verified)
        values = self.values()
        for key, expected in ORIGINAL.items():
            self.assertEqual(values[key], {AC: expected, DC: expected}, key)
        self.assertIsNone(self.store.load_state())
        self.assertIsNotNone(self.store.load_baseline(), "the record is kept")

    def test_restore_without_a_baseline_explains_itself(self) -> None:
        with self.assertRaises(QuietGovernorError) as caught:
            self.controller.restore(confirm=lambda plan: True)
        self.assertIn("nothing to undo", str(caught.exception))

    def test_restore_preview_writes_nothing(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        writes_before = len(self.backend.writes)
        result = self.controller.restore(dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(len(self.backend.writes), writes_before)
        self.assertTrue(self.store.load_state().is_governing)  # type: ignore[union-attr]

    def test_declining_restore_keeps_the_profile_applied(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        with self.assertRaises(ActionRefused):
            self.controller.restore(confirm=lambda plan: False)
        self.assertEqual(self.values()["max_state"], {AC: 60, DC: 60})
        state = self.store.load_state()
        assert state is not None
        self.assertTrue(state.is_governing)

    def test_restore_twice_is_harmless(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.controller.restore(confirm=lambda plan: True)
        result = self.controller.restore(confirm=lambda plan: True)
        self.assertFalse(result.changed)
        self.assertEqual(self.values()["max_state"], {AC: 100, DC: 100})

    def switch_plan(self) -> None:
        self.backend.scheme_name = "High performance"
        self.backend.scheme_guid = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

    def test_restore_refuses_after_a_plan_switch(self) -> None:
        # Windows keeps these settings per plan. Restoring into the new plan
        # would undo nothing while claiming success, and would then forget the
        # plan that still holds the cap.
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.switch_plan()
        with self.assertRaises(PlanNotActive) as caught:
            self.controller.restore(confirm=lambda plan: True)
        self.assertIn("Balanced", str(caught.exception))
        self.assertIn("--into-active-plan", str(caught.exception))
        # Nothing written, and the machine is still governed.
        self.assertEqual(self.values()["max_state"], {AC: 60, DC: 60})
        self.assertTrue(self.controller.status().governing)

    def test_restore_preview_explains_the_plan_switch_without_changing_anything(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.switch_plan()
        result = self.controller.restore(dry_run=True)
        self.assertFalse(result.changed)
        self.assertTrue(any("recorded in power plan" in note for note in result.plan.notes))
        self.assertEqual(self.values()["max_state"], {AC: 60, DC: 60})

    def test_restore_into_the_active_plan_when_asked(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.switch_plan()
        result = self.controller.restore(confirm=lambda plan: True, into_active_plan=True)
        self.assertTrue(result.changed)
        self.assertTrue(any("--into-active-plan" in note for note in result.plan.notes))
        self.assertEqual(self.values()["max_state"], {AC: 100, DC: 100})
        self.assertFalse(self.controller.status().governing)

    def test_switching_back_and_restoring_finishes_the_job(self) -> None:
        # After --into-active-plan the baseline is kept, so returning to the plan
        # that was changed still undoes the cap there.
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.switch_plan()
        self.controller.restore(confirm=lambda plan: True, into_active_plan=True)
        self.backend.scheme_name = "Balanced"
        self.backend.scheme_guid = "381b4222-f694-41f0-9685-ff5bb260df2e"
        self.controller.restore(confirm=lambda plan: True)
        self.assertEqual(self.values()["max_state"], {AC: 100, DC: 100})


class TestStatus(ControllerTestCase):
    def test_status_before_anything(self) -> None:
        status = self.controller.status()
        self.assertFalse(status.governing)
        self.assertIsNone(status.state)
        self.assertIsNone(status.baseline)
        self.assertEqual(status.drift, ())

    def test_status_reports_what_is_managed(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        status = self.controller.status()
        self.assertTrue(status.governing)
        assert status.state is not None
        self.assertEqual(status.state.profile_name, "quiet")
        self.assertEqual(status.drift, ())

    def test_drift_is_detected_when_something_else_changes_a_value(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.backend.write("max_state", AC, 100)  # e.g. Windows or another tool
        status = self.controller.status()
        self.assertEqual(len(status.drift), 1)
        key, source, live, expected = status.drift[0]
        self.assertEqual((key, source, live, expected), ("max_state", AC, 100, 60))

    def test_reapplying_clears_drift(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.backend.write("max_state", AC, 100)
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.assertEqual(self.controller.status().drift, ())


class TestUserProfiles(ControllerTestCase):
    def test_saved_profile_only_overrides_what_it_names(self) -> None:
        self.controller.save_profile("meeting", {"max_state": {AC: 65, DC: 65}})
        result = self.controller.apply("meeting", confirm=lambda plan: True)
        self.assertEqual({change.setting_key for change in result.applied}, {"max_state"})
        values = self.values()
        self.assertEqual(values["max_state"], {AC: 65, DC: 65})
        self.assertEqual(values["boost_mode"], {AC: 1, DC: 1}, "boost must be left alone")
        self.assertEqual(values["min_state"], {AC: 5, DC: 5}, "minimum must be left alone")

    def test_saved_profiles_do_not_show_inherited_values_as_targets(self) -> None:
        self.controller.save_profile("meeting", {"max_state": {AC: 65, DC: 65}})
        plan = self.controller.plan("meeting")
        self.assertEqual(len(plan.changes), 2)
        self.assertEqual(plan.unchanged, ())

    def test_saved_profile_scope(self) -> None:
        self.controller.save_profile("mains", {"max_state": {AC: 65}}, scope="ac")
        result = self.controller.apply("mains", confirm=lambda plan: True)
        self.assertTrue(all(change.source == AC for change in result.applied))

    def test_profiles_combine_built_in_and_saved(self) -> None:
        self.controller.save_profile("meeting", {"max_state": {AC: 65, DC: 65}})
        profiles = self.controller.profiles()
        self.assertIn("meeting", profiles)
        self.assertIn("quiet", profiles)
        self.assertEqual(profiles["meeting"].origin, "user")

    def test_saved_profile_survives_a_new_controller(self) -> None:
        self.controller.save_profile("meeting", {"max_state": {AC: 65, DC: 65}})
        fresh = Controller(SimulatedBackend(), StateStore(self.state_dir))
        self.assertEqual(fresh.get_profile("meeting").value_for("max_state", AC), 65)

    def test_delete_saved_profile(self) -> None:
        self.controller.save_profile("meeting", {"max_state": {AC: 65, DC: 65}})
        self.controller.delete_profile("meeting")
        self.assertNotIn("meeting", self.controller.profiles())

    def test_built_in_profiles_cannot_be_deleted(self) -> None:
        with self.assertRaises(QuietGovernorError) as caught:
            self.controller.delete_profile("quiet")
        self.assertIn("built-in", str(caught.exception))

    def test_saving_under_a_built_in_name_is_refused(self) -> None:
        # Otherwise the built-in would be shadowed everywhere and the saved
        # profile could never be removed by name.
        for name in ("quiet", "Quiet", "restore"):
            with self.subTest(name=name):
                with self.assertRaises(QuietGovernorError) as caught:
                    self.controller.save_profile(name, {"max_state": {AC: 50, DC: 50}})
                message = str(caught.exception)
                self.assertIn("built-in profile", message)
                self.assertIn("another name", message)
        self.assertEqual(self.store.load_user_profiles(), {})
        self.assertEqual(self.controller.get_profile("quiet").origin, "built-in")

    def test_invalid_profile_names_are_refused(self) -> None:
        with self.assertRaises(QuietGovernorError) as caught:
            self.controller.save_profile("has space", {"max_state": {AC: 50, DC: 50}})
        self.assertIn("cannot be a profile name", str(caught.exception))

    def test_a_legacy_shadowing_profile_can_still_be_deleted(self) -> None:
        # Written by 0.2.0, before names were checked against built-ins.
        self.store.save_user_profiles(
            {"quiet": {"name": "quiet", "description": "old", "values": {"max_state": {AC: 50}}, "origin": "user"}}
        )
        self.assertEqual(self.controller.get_profile("quiet").origin, "user")
        self.controller.delete_profile("quiet")
        self.assertEqual(self.controller.get_profile("quiet").origin, "built-in")

    def test_deleting_a_missing_profile_explains_itself(self) -> None:
        with self.assertRaises(QuietGovernorError) as caught:
            self.controller.delete_profile("ghost")
        self.assertIn("no saved profile", str(caught.exception))

    def test_invalid_saved_values_are_refused(self) -> None:
        with self.assertRaises(QuietGovernorError):
            self.controller.save_profile("bad", {"max_state": {AC: 5, DC: 5}})

    def test_unknown_profile_lists_the_options(self) -> None:
        with self.assertRaises(QuietGovernorError) as caught:
            self.controller.get_profile("silent")
        self.assertIn("quiet", str(caught.exception))

    def test_corrupt_saved_profile_is_reported_with_the_file(self) -> None:
        self.store.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.profiles_path.write_text(
            '{"kind": "profiles", "schema_version": 1, '
            '"data": {"profiles": {"odd": {"name": "odd", "values": {"max_state": {"AC": 999}}}}}}',
            encoding="utf-8",
        )
        with self.assertRaises(QuietGovernorError) as caught:
            self.controller.profiles()
        self.assertIn("profiles.json", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestPlanSwitchDuringConfirmation(ControllerTestCase):
    def test_a_plan_switch_during_the_question_aborts_the_apply(self) -> None:
        backend = self.backend

        def switch_plan(plan) -> bool:
            backend.scheme_name = "High performance"
            backend.scheme_guid = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
            return True

        with self.assertRaises(BackendError) as caught:
            self.controller.apply("quiet", confirm=switch_plan)
        self.assertIn("active power plan changed", str(caught.exception))
        self.assertEqual(self.backend.writes, [])

    def test_restore_also_refuses_a_stale_plan(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)

        def meddle(plan) -> bool:
            self.backend.write("max_state", DC, 90)
            return True

        with self.assertRaises(BackendError) as caught:
            self.controller.restore(confirm=meddle)
        self.assertIn("changed while this plan was waiting", str(caught.exception))
        # Still governed: the refused restore changed nothing.
        state = self.store.load_state()
        assert state is not None
        self.assertTrue(state.is_governing)


class TestPlanSwitchReporting(ControllerTestCase):
    """Switching power plans is not the same as something overriding a value."""

    def switch_plan(self, name: str = "High performance", guid: str = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c") -> None:
        self.backend.scheme_name = name
        self.backend.scheme_guid = guid

    def test_same_plan_is_not_reported_as_a_change(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        status = self.controller.status()
        self.assertFalse(status.plan_changed)
        self.assertTrue(status.governing)

    def test_switching_plans_is_reported_as_a_plan_change(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.switch_plan()
        status = self.controller.status()
        self.assertTrue(status.plan_changed)
        assert status.state is not None
        self.assertEqual(status.state.scheme_name, "Balanced")
        self.assertEqual(status.scheme_name, "High performance")

    def test_no_state_means_no_plan_change(self) -> None:
        self.switch_plan()
        self.assertFalse(self.controller.status().plan_changed)

    def test_drift_is_still_measured_but_the_cause_is_named(self) -> None:
        # The values in the new plan differ from what was managed in the old one.
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.backend._values["max_state"] = {AC: 100, DC: 100}
        self.switch_plan()
        status = self.controller.status()
        self.assertTrue(status.plan_changed)
        self.assertEqual(len(status.drift), 2, "the difference is still reported")

    def test_reapplying_to_the_new_plan_clears_the_flag(self) -> None:
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.switch_plan()
        self.controller.apply("quiet", confirm=lambda plan: True)
        status = self.controller.status()
        self.assertFalse(status.plan_changed)
        self.assertTrue(status.governing)
        assert status.state is not None
        self.assertEqual(status.state.scheme_name, "High performance")

    def test_reapplying_when_the_new_plan_already_matches_repoints_the_record(self) -> None:
        # The profile's values already hold in the new plan, so apply has nothing
        # to write. The record must still move to the active plan, or status
        # would keep advising an apply that does nothing.
        self.controller.apply("quiet", confirm=lambda plan: True)
        self.switch_plan()
        result = self.controller.apply("quiet", confirm=lambda plan: True)
        self.assertFalse(result.changed)
        status = self.controller.status()
        self.assertFalse(status.plan_changed)
        assert status.state is not None
        self.assertEqual(status.state.scheme_name, "High performance")
        self.assertTrue(status.state.is_governing)
