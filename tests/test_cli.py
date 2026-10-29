"""The command line: exit codes, output, and refusing to act without consent."""

from __future__ import annotations

import builtins
import json
import sys
import unittest
from unittest import mock

from tests import TtyStdin, run_cli


def simulate(*args: str) -> tuple[int, str, str]:
    return run_cli(["--simulate", *args])


class TestHelp(unittest.TestCase):
    def test_no_command_prints_help(self) -> None:
        code, out, _err = run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("Keep a Windows PC quiet", out)
        self.assertIn("COMMAND", out)

    def test_version(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            run_cli(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_profile_without_action_prints_profile_help(self) -> None:
        code, out, _err = run_cli(["profile"])
        self.assertEqual(code, 2)
        self.assertIn("set", out)
        self.assertIn("delete", out)

    def test_every_command_has_help(self) -> None:
        # --help exits argparse-style with status 0, after printing to stdout.
        for command in ("status", "profiles", "plan", "apply", "restore"):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as caught:
                    run_cli([command, "--help"])
                self.assertEqual(caught.exception.code, 0)


class TestUsageErrors(unittest.TestCase):
    def test_unknown_profile_is_a_clean_error(self) -> None:
        code, _out, err = simulate("plan", "--profile", "silent")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Error: "), err)
        self.assertIn("Available profiles", err)
        self.assertNotIn("Traceback", err)

    def test_out_of_range_value_is_a_clean_error(self) -> None:
        code, _out, err = simulate("profile", "set", "tiny", "--max-state", "5")
        self.assertEqual(code, 1)
        self.assertIn("between 20 and 100", err)
        self.assertNotIn("Traceback", err)

    def test_conflicting_bounds_are_a_clean_error(self) -> None:
        code, _out, err = simulate(
            "profile", "set", "odd", "--max-state", "40", "--min-state", "45"
        )
        self.assertEqual(code, 1)
        self.assertIn("higher than maximum", err)

    def test_profile_set_without_values_explains_what_to_do(self) -> None:
        code, _out, err = simulate("profile", "set", "nothing")
        self.assertEqual(code, 1)
        self.assertIn("--max-state", err)

    def test_boolean_boost_words_are_accepted(self) -> None:
        code, out, err = simulate("profile", "set", "b", "--boost", "off")
        self.assertEqual(code, 0, err)
        self.assertIn("boost mode", out.lower())


class TestSimulatedFlow(unittest.TestCase):
    """End to end through the CLI, using a temporary state folder."""

    def setUp(self) -> None:
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = os.environ.get("QUIETGOVERNOR_STATE_DIR")
        os.environ["QUIETGOVERNOR_STATE_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        import os

        if self._env is None:
            os.environ.pop("QUIETGOVERNOR_STATE_DIR", None)
        else:
            os.environ["QUIETGOVERNOR_STATE_DIR"] = self._env

    def test_status_before_and_after_apply(self) -> None:
        code, out, _err = simulate("status")
        self.assertEqual(code, 0)
        self.assertIn("Not managing this PC", out)
        self.assertIn("Simulation mode", out)

        code, out, err = simulate("apply", "--profile", "quiet", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Restore point recorded", out)
        self.assertIn("Changed 6 setting(s)", out)

        code, out, _err = simulate("status")
        self.assertEqual(code, 0)
        self.assertIn("Managing: quiet profile", out)
        self.assertIn("Maximum processor state  plugged-in: 60%", out)

    def test_plan_is_a_preview_only(self) -> None:
        code, out, _err = simulate("plan", "--profile", "quiet")
        self.assertEqual(code, 0)
        self.assertIn("would change 6 settings", out)
        self.assertIn("This is a preview", out)
        code, out, _err = simulate("status")
        self.assertIn("Not managing this PC", out)

    def test_apply_preview_does_not_change_anything(self) -> None:
        code, out, _err = simulate("apply", "--profile", "quiet", "--preview")
        self.assertEqual(code, 0)
        self.assertIn("Preview only", out)
        code, out, _err = simulate("status")
        self.assertIn("plugged-in: 100%", out)

    def test_apply_without_a_terminal_refuses_instead_of_guessing(self) -> None:
        # stdin in a test run is not a tty, so confirmation is impossible.
        code, out, err = simulate("apply", "--profile", "quiet")
        self.assertEqual(code, 0)
        # The plan is still shown, so the user can see what was refused.
        self.assertIn("would change", out)
        self.assertIn("no terminal to confirm", err)
        code, out, _err = simulate("status")
        self.assertIn("Not managing this PC", out)

    def test_answering_no_changes_nothing(self) -> None:
        with mock.patch.object(sys, "stdin", TtyStdin("no\n")), mock.patch.object(
            builtins, "input", return_value="no"
        ):
            code, out, err = run_cli(
                ["--simulate", "apply", "--profile", "quiet"]
            )
        self.assertEqual(code, 0)
        self.assertIn("would change", out)
        self.assertIn("Nothing was changed", err)
        code, out, _err = simulate("status")
        self.assertIn("Not managing this PC", out)

    def test_answering_yes_applies(self) -> None:
        with mock.patch.object(sys, "stdin", TtyStdin("yes\n")), mock.patch.object(
            builtins, "input", return_value="yes"
        ):
            code, out, err = run_cli(["--simulate", "apply", "--profile", "quiet"])
        self.assertEqual(code, 0, err)
        self.assertIn("Changed 6 setting(s)", out)

    def test_restore_after_apply(self) -> None:
        simulate("apply", "--profile", "quiet", "--yes")
        code, out, err = simulate("restore", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("original processor settings are back in place", out)
        code, out, _err = simulate("status")
        self.assertIn("Not managing this PC", out)
        self.assertIn("plugged-in: 100%", out)

    def test_restore_without_a_restore_point(self) -> None:
        code, _out, err = simulate("restore", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("nothing to undo", err)

    def test_saved_profile_then_apply(self) -> None:
        code, out, err = simulate(
            "profile", "set", "meeting", "--max-state", "70", "--description", "calls"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Saved profile 'meeting'", out)
        code, out, _err = simulate("profiles")
        self.assertEqual(code, 0)
        self.assertIn("meeting", out)
        code, out, err = simulate("apply", "--profile", "meeting", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Changed 2 setting(s)", out)
        code, out, _err = simulate("status")
        self.assertIn("plugged-in: 70%", out)
        self.assertIn("battery: 70%", out)

    def test_profile_show_and_delete(self) -> None:
        simulate("profile", "set", "meeting", "--max-state", "70")
        code, out, _err = simulate("profile", "show", "meeting")
        self.assertEqual(code, 0)
        self.assertIn("70%", out)
        code, out, _err = simulate("profile", "delete", "meeting")
        self.assertEqual(code, 0)
        self.assertIn("Deleted", out)
        code, _out, err = simulate("profile", "show", "meeting")
        self.assertEqual(code, 1)
        self.assertIn("no profile called", err.lower())

    def test_json_status_is_parseable(self) -> None:
        simulate("apply", "--profile", "quiet", "--yes")
        code, out, _err = simulate("status", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["governing"])
        self.assertEqual(payload["profile"], "quiet")
        self.assertEqual(payload["settings"]["max_state"]["ac"], 60)
        self.assertEqual(payload["drift"], [])
        self.assertIsNotNone(payload["restore_point"])

    def test_json_plan_and_apply(self) -> None:
        code, out, _err = simulate("plan", "--profile", "quiet", "--json")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(len(plan["changes"]), 6)
        self.assertEqual(plan["changes"][0]["setting"], "boost_mode")

        code, out, err = simulate("apply", "--profile", "quiet", "--yes", "--json")
        self.assertEqual(code, 0, err)
        applied = json.loads(out)
        self.assertTrue(applied["verified"])
        self.assertTrue(applied["baseline_created"])
        self.assertEqual(len(applied["applied"]), 6)

    def test_json_is_not_decorated_with_colour(self) -> None:
        code, out, _err = simulate("status", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("\033[", out)

    def test_no_color_is_honoured(self) -> None:
        code, out, _err = run_cli(["--simulate", "--no-color", "status"])
        self.assertEqual(code, 0)
        self.assertNotIn("\033[", out)

    def test_scope_ac_only(self) -> None:
        code, out, err = simulate("apply", "--profile", "quiet", "--scope", "ac", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Changed 3 setting(s)", out)
        code, out, _err = simulate("status")
        self.assertIn("plugged-in: 60%", out)
        self.assertIn("battery: 100%", out)

    def test_drift_is_reported(self) -> None:
        import pathlib

        simulate("apply", "--profile", "quiet", "--yes")
        machine = pathlib.Path(self._tmp.name) / "simulated" / "simulated-machine.json"
        payload = json.loads(machine.read_text(encoding="utf-8"))
        payload["values"]["max_state"]["AC"] = 100
        machine.write_text(json.dumps(payload), encoding="utf-8")
        code, out, _err = simulate("status")
        self.assertEqual(code, 0)
        self.assertIn("no longer match", out)
        self.assertIn("is 100%, expected 60%", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
