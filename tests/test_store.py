"""Storage: atomic writes, schema guards, and keeping the first baseline."""

from __future__ import annotations

import json
import os
import stat
import unittest

from tests import TempDirTestCase

from quietgovernor.backend import SimulatedBackend, capture_baseline
from quietgovernor.errors import StoreError
from quietgovernor.policy import AC, DC
from quietgovernor.store import AppliedState, StateStore, atomic_write_text, default_state_dir


class TestDefaultLocation(TempDirTestCase):
    def test_state_dir_uses_the_environment(self) -> None:
        self.assertEqual(default_state_dir(), self.state_dir)

    def test_localappdata_is_used_on_windows(self) -> None:
        os.environ.pop("QUIETGOVERNOR_STATE_DIR", None)
        os.environ["LOCALAPPDATA"] = str(self.tmp_path / "AppData")
        self.assertEqual(
            default_state_dir(), self.tmp_path / "AppData" / "QuietGovernor"
        )

    def test_explicit_directory_wins(self) -> None:
        store = StateStore(self.tmp_path / "elsewhere")
        self.assertEqual(store.directory, self.tmp_path / "elsewhere")


class TestAtomicWrite(TempDirTestCase):
    def test_file_contents_and_permissions(self) -> None:
        target = self.tmp_path / "nested" / "file.txt"
        atomic_write_text(target, "hello\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
        if os.name == "posix":
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertEqual(mode, 0o600, "state files must not be world readable")

    def test_no_temporary_files_are_left_behind(self) -> None:
        target = self.tmp_path / "file.txt"
        atomic_write_text(target, "one")
        atomic_write_text(target, "two")
        self.assertEqual(sorted(p.name for p in self.tmp_path.iterdir()), ["file.txt"])

    def test_previous_content_survives_a_failed_write(self) -> None:
        target = self.tmp_path / "file.txt"
        atomic_write_text(target, "first")
        # A directory where the file should go makes the final replace fail.
        target.unlink()
        target.mkdir()
        with self.assertRaises(OSError):
            atomic_write_text(target, "second")
        self.assertTrue(target.is_dir())
        self.assertEqual([p.name for p in self.tmp_path.iterdir()], ["file.txt"])


class TestDocuments(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = StateStore(self.state_dir)

    def test_absent_files_read_as_none(self) -> None:
        self.assertIsNone(self.store.load_baseline())
        self.assertIsNone(self.store.load_state())
        self.assertEqual(self.store.load_user_profiles(), {})

    def test_baseline_round_trip(self) -> None:
        baseline = capture_baseline(SimulatedBackend({"max_state": {AC: 90, DC: 80}}))
        self.store.save_baseline(baseline)
        loaded = self.store.load_baseline()
        assert loaded is not None
        self.assertEqual(loaded.readings["max_state"].value_for(DC), 80)

    def test_ensure_baseline_keeps_the_first_capture(self) -> None:
        first = capture_baseline(SimulatedBackend({"max_state": {AC: 90, DC: 90}}))
        self.store.ensure_baseline(first)
        second = capture_baseline(SimulatedBackend({"max_state": {AC: 40, DC: 40}}))
        kept = self.store.ensure_baseline(second)
        self.assertEqual(kept.readings["max_state"].value_for(AC), 90)
        loaded = self.store.load_baseline()
        assert loaded is not None
        self.assertEqual(loaded.readings["max_state"].value_for(AC), 90)

    def test_json_shape_is_documented_and_stable(self) -> None:
        self.store.save_baseline(capture_baseline(SimulatedBackend()))
        payload = json.loads(self.store.baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "baseline")
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("readings", payload["data"])

    def test_corrupt_json_names_the_file_and_the_fix(self) -> None:
        self.store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.baseline_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StoreError) as caught:
            self.store.load_baseline()
        message = str(caught.exception)
        self.assertIn(str(self.store.baseline_path), message)
        self.assertIn("Move or delete it", message)

    def test_wrong_kind_is_refused(self) -> None:
        self.store.save_state(AppliedState(profile_name="quiet"))
        self.store.state_path.replace(self.store.baseline_path)
        with self.assertRaises(StoreError) as caught:
            self.store.load_baseline()
        self.assertIn("expected", str(caught.exception))

    def test_future_schema_version_is_refused(self) -> None:
        self.store.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.state_path.write_text(
            json.dumps({"kind": "state", "schema_version": 7, "data": {}}), encoding="utf-8"
        )
        with self.assertRaises(StoreError) as caught:
            self.store.load_state()
        self.assertIn("schema version 7", str(caught.exception))

    def test_baseline_with_future_schema_is_refused_by_the_envelope(self) -> None:
        baseline = capture_baseline(SimulatedBackend()).to_dict()
        self.store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.baseline_path.write_text(
            json.dumps({"kind": "baseline", "schema_version": 2, "data": baseline}),
            encoding="utf-8",
        )
        with self.assertRaises(StoreError):
            self.store.load_baseline()


class TestAppliedState(TempDirTestCase):
    def test_round_trip(self) -> None:
        state = AppliedState(
            profile_name="quiet",
            managed_values={"max_state": {AC: 60, DC: 60}},
            scheme_guid="guid",
            scheme_name="Balanced",
            profile_origin="built-in",
        )
        restored = AppliedState.from_dict(state.to_dict())
        self.assertEqual(restored.managed_value("max_state", DC), 60)
        self.assertTrue(restored.is_governing)

    def test_invalid_managed_values_are_refused(self) -> None:
        for bad in (
            {"profile_name": ""},
            {"profile_name": "x", "managed_values": {"nope": {AC: 5}}},
            {"profile_name": "x", "managed_values": {"max_state": {AC: 500}}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                AppliedState.from_dict(bad)

    def test_clear_state_is_idempotent(self) -> None:
        store = StateStore(self.state_dir)
        store.clear_state()
        store.save_state(AppliedState(profile_name="quiet", managed_values={"max_state": {AC: 60}}))
        store.clear_state()
        self.assertIsNone(store.load_state())

    def test_deleting_state_keeps_the_baseline(self) -> None:
        store = StateStore(self.state_dir)
        store.save_baseline(capture_baseline(SimulatedBackend()))
        store.save_state(AppliedState(profile_name="quiet", managed_values={"max_state": {AC: 60}}))
        store.clear_state()
        self.assertIsNotNone(store.load_baseline())


class TestUserProfiles(TempDirTestCase):
    def test_round_trip(self) -> None:
        store = StateStore(self.state_dir)
        store.save_user_profiles({"night": {"name": "night", "values": {"max_state": {AC: 50}}}})
        self.assertEqual(store.load_user_profiles()["night"]["name"], "night")

    def test_bad_profiles_file_is_reported(self) -> None:
        store = StateStore(self.state_dir)
        store.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        store.profiles_path.write_text(
            json.dumps({"kind": "profiles", "schema_version": 1, "data": {"profiles": []}}),
            encoding="utf-8",
        )
        with self.assertRaises(StoreError) as caught:
            store.load_user_profiles()
        self.assertIn("not an object", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
