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
        # 1, not 0: the caller asked for a change and could not approve it, so a
        # script must be able to tell that nothing happened.
        self.assertEqual(code, 1)
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


class TestAutostartCommands(unittest.TestCase):
    """Auto-start through the CLI, in simulation mode."""

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

    def test_autostart_without_action_prints_help(self) -> None:
        code, out, _err = simulate("autostart")
        self.assertEqual(code, 2)
        self.assertIn("install", out)
        self.assertIn("remove", out)
        self.assertIn("run", out)

    def test_preview_creates_nothing(self) -> None:
        code, out, _err = simulate("autostart", "install", "--preview")
        self.assertEqual(code, 0)
        self.assertIn("Preview only", out)
        self.assertIn("administrator", out)
        code, out, _err = simulate("status")
        self.assertIn("Not installed", out)

    def test_install_then_status_then_remove(self) -> None:
        code, out, err = simulate("autostart", "install", "--profile", "quiet", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Created the sign-in task", out)

        code, out, _err = simulate("status")
        self.assertIn("Installed: applies the 'quiet' profile at sign-in", out)
        self.assertIn("HighestAvailable", out)
        self.assertIn("Last run:  none yet", out)

        code, out, err = simulate("autostart", "remove", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed the sign-in task", out)
        self.assertIn("Your current processor settings are unchanged", out)
        code, out, _err = simulate("status")
        self.assertIn("Not installed", out)

    def test_install_requires_confirmation_without_yes(self) -> None:
        # No terminal in a test run, so it must refuse rather than assume.
        code, out, err = simulate("autostart", "install")
        self.assertEqual(code, 1)
        self.assertIn("no terminal to confirm", err)
        code, out, _err = simulate("status")
        self.assertIn("Not installed", out)

    def test_declining_leaves_no_task(self) -> None:
        with mock.patch.object(sys, "stdin", TtyStdin("no\n")), mock.patch.object(
            builtins, "input", return_value="no"
        ):
            code, _out, err = run_cli(["--simulate", "autostart", "install"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing was changed", err)
        code, out, _err = simulate("status")
        self.assertIn("Not installed", out)

    def test_unknown_profile_is_refused_before_creating_anything(self) -> None:
        code, _out, err = simulate("autostart", "install", "--profile", "nope", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no profile called", err)
        self.assertIn("nothing on this PC was changed", err)
        code, out, _err = simulate("status")
        self.assertIn("Not installed", out)

    def test_run_applies_the_profile_and_records_the_outcome(self) -> None:
        simulate("autostart", "install", "--profile", "quiet", "--yes")
        code, out, _err = simulate("autostart", "run")
        self.assertEqual(code, 0)
        self.assertIn("Applied 6 setting(s)", out)
        code, out, _err = simulate("status")
        self.assertIn("Last run:  succeeded", out)
        self.assertIn("plugged-in: 60%", out)

    def test_run_with_json_prints_json(self) -> None:
        simulate("autostart", "install", "--profile", "quiet", "--yes")
        code, out, _err = simulate("autostart", "run", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile"], "quiet")

    def test_a_failing_run_is_recorded_and_exits_nonzero(self) -> None:
        # A profile that vanishes after install is the realistic failure: the
        # task runs unattended, so the outcome must land in the record.
        simulate("autostart", "install", "--profile", "quiet", "--yes")
        simulate("profile", "set", "temp", "--max-state", "70")
        simulate("autostart", "install", "--profile", "temp", "--yes")
        code, _out, err = simulate("profile", "delete", "temp")
        self.assertEqual(code, 0, err)
        code, _out, err = simulate("autostart", "run", "--profile", "temp")
        self.assertEqual(code, 1)
        self.assertIn("no profile called", err + _out)
        code, out, _err = simulate("status")
        self.assertIn("Last run:  failed", out)

    def test_remove_when_nothing_is_installed(self) -> None:
        code, _out, err = simulate("autostart", "remove", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("nothing to remove", err)

    def test_json_status_includes_autostart(self) -> None:
        simulate("autostart", "install", "--profile", "balanced", "--yes")
        code, out, _err = simulate("status", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["autostart"]["installed"])
        self.assertEqual(payload["autostart"]["profile"], "balanced")
        self.assertEqual(payload["autostart"]["run_level"], "HighestAvailable")
        self.assertEqual(payload["autostart"]["problems"], [])

    def test_json_install_is_parseable(self) -> None:
        code, out, err = simulate("autostart", "install", "--profile", "quiet", "--yes", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["created"])
        self.assertFalse(payload["blocked"])
        self.assertTrue(any(check["passed"] for check in payload["checks"]))

    def test_installing_does_not_change_power_settings(self) -> None:
        code, out, err = simulate("autostart", "install", "--profile", "quiet", "--yes")
        self.assertEqual(code, 0, err)
        code, out, _err = simulate("status")
        self.assertIn("Not managing this PC", out)
        self.assertIn("plugged-in: 100%", out)


class TestAutostartWithoutScheduler(unittest.TestCase):
    """On a system with no Task Scheduler, status must still work."""

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

    def test_install_explains_that_this_pc_cannot_host_a_task(self) -> None:
        with mock.patch("quietgovernor.cli.build_autostart", return_value=None):
            code, _out, err = run_cli(["--simulate", "autostart", "install", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("Auto-start cannot be managed on this PC", err)

    def test_status_still_reports_power_settings(self) -> None:
        with mock.patch("quietgovernor.cli.build_autostart", return_value=None):
            code, out, _err = run_cli(["--simulate", "status"])
        self.assertEqual(code, 0)
        self.assertIn("Processor settings", out)
        self.assertIn("Unavailable on this system", out)

    def test_json_status_reports_unavailable_instead_of_failing(self) -> None:
        with mock.patch("quietgovernor.cli.build_autostart", return_value=None):
            code, out, _err = run_cli(["--simulate", "status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["autostart"]["available"])
        self.assertFalse(payload["autostart"]["installed"])


class TestStatusJsonEnvironment(unittest.TestCase):
    def test_real_mode_reports_elevation(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            saved = os.environ.get("QUIETGOVERNOR_STATE_DIR")
            os.environ["QUIETGOVERNOR_STATE_DIR"] = tmp
            try:
                with mock.patch("quietgovernor.cli.build_backend") as backend:
                    from quietgovernor.backend import SimulatedBackend

                    backend.return_value = SimulatedBackend()
                    with mock.patch("quietgovernor.cli._elevated", return_value=False):
                        code, out, _err = run_cli(["status", "--json"])
            finally:
                if saved is None:
                    os.environ.pop("QUIETGOVERNOR_STATE_DIR", None)
                else:
                    os.environ["QUIETGOVERNOR_STATE_DIR"] = saved
        self.assertEqual(code, 0)
        self.assertIs(json.loads(out)["elevated"], False)

    def test_simulation_reports_elevation_as_not_applicable(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            saved = os.environ.get("QUIETGOVERNOR_STATE_DIR")
            os.environ["QUIETGOVERNOR_STATE_DIR"] = tmp
            try:
                code, out, _err = run_cli(["--simulate", "status", "--json"])
            finally:
                if saved is None:
                    os.environ.pop("QUIETGOVERNOR_STATE_DIR", None)
                else:
                    os.environ["QUIETGOVERNOR_STATE_DIR"] = saved
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["elevated"])


class TestProfileNameAndPlanReporting(unittest.TestCase):
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

    def test_saving_under_a_built_in_name_explains_the_fix(self) -> None:
        code, _out, err = simulate("profile", "set", "quiet", "--max-state", "50")
        self.assertEqual(code, 1)
        self.assertIn("built-in profile", err)
        self.assertIn("quiet-mine", err)
        # The built-in is untouched.
        code, out, _err = simulate("profile", "show", "quiet")
        self.assertEqual(code, 0)
        self.assertIn("Origin: built-in", out)

    def test_a_name_with_spaces_is_refused_with_a_suggestion(self) -> None:
        code, _out, err = simulate("profile", "set", "quiet mode", "--max-state", "50")
        self.assertEqual(code, 1)
        self.assertIn("cannot be a profile name", err)
        self.assertIn("quiet-mode", err)

    def test_deleting_a_built_in_still_says_why(self) -> None:
        code, _out, err = simulate("profile", "delete", "restore")
        self.assertEqual(code, 1)
        self.assertIn("built-in", err)

    def test_status_names_a_power_plan_change_instead_of_drift(self) -> None:
        import json as jsonlib
        import pathlib

        simulate("apply", "--profile", "quiet", "--yes")
        machine = pathlib.Path(self._tmp.name) / "simulated" / "simulated-machine.json"
        payload = jsonlib.loads(machine.read_text(encoding="utf-8"))
        payload["scheme_guid"] = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
        payload["scheme_name"] = "High performance"
        machine.write_text(jsonlib.dumps(payload), encoding="utf-8")

        code, out, _err = simulate("status")
        self.assertEqual(code, 0)
        self.assertIn("not the one this profile was applied to", out)
        self.assertIn("Applied to: Balanced", out)
        self.assertIn("Active now: High performance", out)
        self.assertNotIn("something else changed them", out)
        self.assertNotIn("Every managed setting matches", out)

        code, out, _err = simulate("status", "--json")
        payload = jsonlib.loads(out)
        self.assertTrue(payload["plan_changed"])
        self.assertEqual(payload["applied_to_plan"]["name"], "Balanced")

    def test_reapplying_clears_the_plan_change_notice(self) -> None:
        import json as jsonlib
        import pathlib

        simulate("apply", "--profile", "quiet", "--yes")
        machine = pathlib.Path(self._tmp.name) / "simulated" / "simulated-machine.json"
        payload = jsonlib.loads(machine.read_text(encoding="utf-8"))
        payload["scheme_guid"] = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
        payload["scheme_name"] = "High performance"
        machine.write_text(jsonlib.dumps(payload), encoding="utf-8")

        simulate("apply", "--profile", "quiet", "--yes")
        code, out, _err = simulate("status")
        self.assertEqual(code, 0)
        self.assertNotIn("not the one this profile was applied to", out)


class TestConsentExitCodes(unittest.TestCase):
    """A change that could not be approved must not exit 0."""

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

    def test_apply_without_a_terminal_exits_non_zero(self) -> None:
        code, _out, err = simulate("apply", "--profile", "quiet")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", err)

    def test_apply_json_without_yes_exits_non_zero(self) -> None:
        code, out, err = simulate("apply", "--profile", "quiet", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "", "stdout must stay pure JSON")
        self.assertIn("--json output cannot answer", err)

    def test_autostart_install_without_a_terminal_exits_non_zero(self) -> None:
        code, _out, err = simulate("autostart", "install", "--profile", "quiet")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", err)

    def test_declining_at_the_keyboard_is_still_success(self) -> None:
        # A person answered; that is a deliberate choice, not a failure.
        with mock.patch.object(sys, "stdin", TtyStdin("no\n")), mock.patch.object(
            builtins, "input", return_value="no"
        ):
            code, _out, err = run_cli(["--simulate", "apply", "--profile", "quiet"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing was changed", err)

    def test_nothing_is_written_when_consent_is_unavailable(self) -> None:
        simulate("apply", "--profile", "quiet")
        code, out, _err = simulate("status")
        self.assertEqual(code, 0)
        self.assertIn("Not managing this PC", out)


class TestGlobalOptionsAnywhere(unittest.TestCase):
    """The README promises the global options work before or after the command."""

    def test_version_after_the_command(self) -> None:
        for argv in (["status", "--version"], ["autostart", "install", "--version"]):
            with self.assertRaises(SystemExit) as caught:
                run_cli(argv)
            self.assertEqual(caught.exception.code, 0)

    def test_common_options_after_a_nested_subcommand(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.get("QUIETGOVERNOR_STATE_DIR")
            os.environ["QUIETGOVERNOR_STATE_DIR"] = tmp
            try:
                code, out, err = run_cli(["autostart", "install", "--preview", "--simulate"])
                self.assertEqual(code, 0, err)
                self.assertIn("Simulation mode", out)
            finally:
                if env is None:
                    os.environ.pop("QUIETGOVERNOR_STATE_DIR", None)
                else:
                    os.environ["QUIETGOVERNOR_STATE_DIR"] = env

    def test_documented_preview_forms_work(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.get("QUIETGOVERNOR_STATE_DIR")
            os.environ["QUIETGOVERNOR_STATE_DIR"] = tmp
            try:
                for argv in (
                    ["--simulate", "apply", "--profile", "quiet", "--preview"],
                    ["--simulate", "apply", "--profile", "quiet", "--dry-run"],
                    ["--simulate", "autostart", "install", "--preview"],
                    ["--simulate", "autostart", "install", "--dry-run"],
                    ["--simulate", "autostart", "remove", "--preview"],
                ):
                    code, _out, err = run_cli(list(argv))
                    self.assertEqual(code, 0, f"{argv}: {err}")
            finally:
                if env is None:
                    os.environ.pop("QUIETGOVERNOR_STATE_DIR", None)
                else:
                    os.environ["QUIETGOVERNOR_STATE_DIR"] = env
