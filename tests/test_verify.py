"""The verification harness in scripts/verify_windows.py.

Two things are worth testing here: that the harness cannot misreport a run as
evidence when it was a simulation or a non-Windows machine, and that it always
tries to put the machine back even when a check fails midway.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests import REPO_ROOT, TempDirTestCase

SCRIPTS = REPO_ROOT / "scripts"


def load_harness():
    """Import scripts/verify_windows.py by path (scripts/ is not a package)."""
    path = SCRIPTS / "verify_windows.py"
    spec = importlib.util.spec_from_file_location("verify_windows", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before executing: the module uses dataclasses with postponed
    # annotations, which look themselves up in sys.modules during creation.
    sys.modules["verify_windows"] = module
    spec.loader.exec_module(module)
    return module


harness = load_harness()


class FakeCli:
    """Stands in for the installed CLI, including misbehaviour."""

    def __init__(
        self,
        *,
        writes_apply: bool = True,
        restore_fails: bool = False,
        reverts: bool = True,
        guard: str = "trusted",
    ) -> None:
        self.state_dir = None
        self.simulate = False
        self.state_dir_label = "default per-user folder"
        self.writes_apply = writes_apply
        self.restore_fails = restore_fails
        self.reverts = reverts
        self.guard = guard
        self.calls: list[tuple[str, ...]] = []
        self._applied = False
        self._task_installed = False
        self._values = {"max_state": {"ac": 100, "dc": 100}, "boost_mode": {"ac": 1, "dc": 1}}

    # -- the interface the harness uses -----------------------------------

    def preview_text(self) -> str:
        from quietgovernor.autostart import USER_WRITABLE_HELP

        return {
            "trusted": (
                "The interpreter and the QuietGovernor package both live in "
                "administrator-only folders, so the task cannot be hijacked."
            ),
            "refused": USER_WRITABLE_HELP
            + " These paths are in a folder your account can write to: C:\\x. "
            "Nothing was changed.",
            "unknown": (
                "Could not work out which folders are administrator-only on this "
                "system, so QuietGovernor cannot confirm the task would not run code "
                "from a folder your account can write to."
            ),
            "silent": "Plan: create the task at \\QuietGovernor\\ApplyAtLogon.",
        }[self.guard]

    def run(self, *arguments: str, check: bool = False):
        self.calls.append(arguments)
        if arguments[0] == "apply":
            if self.writes_apply:
                self._applied = True
                self._values["max_state"] = {"ac": 60, "dc": 60}
                self._values["boost_mode"] = {"ac": 0, "dc": 0}
            return subprocess.CompletedProcess(list(arguments), 0, "Changed 6 setting(s)", "")
        if arguments[0] == "restore":
            if self.restore_fails:
                return subprocess.CompletedProcess(list(arguments), 1, "", "access denied")
            self._applied = False
            if self.reverts:
                self._values["max_state"] = {"ac": 100, "dc": 100}
                self._values["boost_mode"] = {"ac": 1, "dc": 1}
            return subprocess.CompletedProcess(list(arguments), 0, "Changed 5 setting(s)", "")
        if arguments[0] == "autostart":
            if "--preview" in arguments:
                return subprocess.CompletedProcess(list(arguments), 0, self.preview_text(), "")
            if arguments[1] == "install":
                if self.guard in {"refused", "unknown"} and "--allow-untrusted-paths" not in arguments:
                    return subprocess.CompletedProcess(
                        list(arguments),
                        1,
                        "Auto-start was not installed, and nothing on this PC was changed.",
                        "",
                    )
                self._task_installed = True
                return subprocess.CompletedProcess(list(arguments), 0, "", "")
            if arguments[1] == "remove":
                self._task_installed = False
                return subprocess.CompletedProcess(list(arguments), 0, "", "")
            return subprocess.CompletedProcess(list(arguments), 0, "", "")
        return subprocess.CompletedProcess(list(arguments), 0, "", "")

    def json(self, *arguments: str) -> dict:
        self.calls.append(arguments)
        if "install" in arguments and "autostart" in arguments:
            return {}
        if "plan" in arguments:
            return {
                "changes": [
                    {"setting": "boost_mode", "source": "AC", "target": 0},
                    {"setting": "boost_mode", "source": "DC", "target": 0},
                    {"setting": "max_state", "source": "AC", "target": 60},
                    {"setting": "max_state", "source": "DC", "target": 60},
                ]
            }
        return self.status()

    def status(self) -> dict:
        return {
            "scheme": {"guid": "guid", "name": "Balanced"},
            "elevated": True,
            "governing": self._applied,
            "profile": "quiet" if self._applied else None,
            "settings": {
                key: {"available": True, **values} for key, values in self._values.items()
            },
            "drift": [],
            "restore_point": {"captured_at": "2026-10-29T00:00:00+00:00"},
            "autostart": {
                "available": True,
                "installed": self._task_installed,
                "profile": "quiet" if self._task_installed else None,
                "run_level": "HighestAvailable" if self._task_installed else None,
                "command": "pythonw.exe" if self._task_installed else None,
                "arguments": (
                    "-m quietgovernor autostart run --profile quiet"
                    if self._task_installed
                    else None
                ),
                "problems": [],
            },
        }


class TestJsonSafety(unittest.TestCase):
    def test_tuple_keys_become_readable_strings(self) -> None:
        converted = harness._jsonable({("max_state", "AC"): 60})
        self.assertEqual(converted, {"max_state/AC": 60})

    def test_report_serialises_everything(self) -> None:
        report = harness.Report(mode="real")
        report.add(
            harness.Check(
                "written values match the plan",
                ok=True,
                detail="fine",
                expected={("max_state", "AC"): 60},
                actual=Path("/tmp/x"),
            )
        )
        text = json.dumps(report.to_dict())
        self.assertIn("max_state/AC", text)


class TestVerdicts(unittest.TestCase):
    def report(self, *, mode: str, failures: int = 0, skipped: int = 0) -> "harness.Report":
        report = harness.Report(mode=mode)
        report.environment = {
            "windows_name": "Windows 11",
            "windows_build": 26100,
            "python": "3.12.0",
            "elevated": True,
        }
        for index in range(failures):
            report.add(harness.Check(f"failing {index}", ok=False, detail="broken"))
        for index in range(skipped):
            report.add(
                harness.Check(f"sign-in task {index}", ok=True, skipped=True, detail="skipped")
            )
        report.add(harness.Check("a passing check", ok=True, detail="fine"))
        return report

    def test_a_simulation_never_claims_to_be_evidence(self) -> None:
        markdown = harness.render_markdown(self.report(mode="simulate"))
        self.assertIn("Simulation run", markdown)
        self.assertIn("not evidence", markdown)
        self.assertIn("Nothing to record", markdown)

    def test_all_passed_on_a_real_machine(self) -> None:
        markdown = harness.render_markdown(self.report(mode="real"))
        self.assertIn("All checks passed", markdown)
        self.assertNotIn("Do not record", markdown)

    def test_a_partial_run_says_it_is_partial(self) -> None:
        markdown = harness.render_markdown(self.report(mode="real", skipped=1))
        self.assertIn("partial result", markdown)
        self.assertIn("1 phase(s) skipped", markdown)

    def test_failures_block_recording(self) -> None:
        markdown = harness.render_markdown(self.report(mode="real", failures=2))
        self.assertIn("2 check(s) failed", markdown)
        self.assertIn("Do not record", markdown)

    def test_environment_details_are_recorded(self) -> None:
        markdown = harness.render_markdown(self.report(mode="real"))
        self.assertIn("Windows 11", markdown)
        self.assertIn("26100", markdown)
        self.assertIn("Python 3.12.0", markdown)


class TestGuardVerdict(unittest.TestCase):
    """The detector must agree with the words the product actually prints."""

    def test_trusted(self) -> None:
        text = (
            "The interpreter and the QuietGovernor package both live in "
            "administrator-only folders, so the task cannot be hijacked."
        )
        self.assertEqual(harness.guard_verdict(text), "trusted")

    def test_refused_matches_the_product_message(self) -> None:
        from quietgovernor.autostart import USER_WRITABLE_HELP

        text = USER_WRITABLE_HELP + (
            " These paths are in a folder your account can write to: C:\\Users\\x. "
            "Nothing was changed."
        )
        self.assertEqual(harness.guard_verdict(text), "refused")

    def test_override_warning_reads_as_a_refusal(self) -> None:
        text = (
            "Auto-start will run code that is not in an administrator-only folder: "
            "C:\\Users\\x. Anything that can write there could run code as administrator."
        )
        self.assertEqual(harness.guard_verdict(text), "refused")

    def test_unknown_wins_over_the_phrases_it_contains(self) -> None:
        text = (
            "Could not work out which folders are administrator-only on this system, "
            "so QuietGovernor cannot confirm the task would not run code from a folder "
            "your account can write to."
        )
        self.assertEqual(harness.guard_verdict(text), "unknown")

    def test_no_verdict_at_all(self) -> None:
        self.assertEqual(harness.guard_verdict("Plan: create the task."), "silent")


class TestWritePhaseGuarantees(unittest.TestCase):
    def test_restore_runs_even_when_a_check_fails(self) -> None:
        # The simulated PC ignores the write, so "written values match the plan"
        # must fail; restore must still run.
        cli = FakeCli(writes_apply=False)
        report = harness.Report(mode="real")
        harness.phase_read(cli, report)
        harness.phase_write(cli, report, "quiet")

        failed = [check.name for check in report.failures]
        self.assertIn("written values match the plan", failed)
        self.assertEqual(
            [call[0] for call in cli.calls].count("restore"), 1, "restore must still run"
        )
        self.assertIn("restore exits successfully", [check.name for check in report.checks])

    def test_a_failed_restore_is_reported_with_the_way_back(self) -> None:
        cli = FakeCli(restore_fails=True)
        report = harness.Report(mode="real")
        harness.phase_read(cli, report)
        harness.phase_write(cli, report, "quiet")
        failure = next(
            check for check in report.failures if check.name == "restore ran"
        )
        self.assertIn("quietgovernor restore", failure.detail)
        self.assertIn("recorded in", failure.detail)
        self.assertIn("access denied", failure.detail)

    def test_a_restore_that_does_not_revert_is_caught(self) -> None:
        cli = FakeCli(reverts=False)
        report = harness.Report(mode="real")
        harness.phase_read(cli, report)
        harness.phase_write(cli, report, "quiet")
        self.assertIn("original values are back", [check.name for check in report.failures])

    def test_a_correct_refusal_by_the_guard_is_a_pass(self) -> None:
        # The guard declining on a user-writable Python is the feature working.
        cli = FakeCli(guard="refused")
        report = harness.Report(mode="real")
        harness.phase_autostart(cli, report, install=True, profile="quiet")
        guard = next(c for c in report.checks if c.name.startswith("the privilege guard"))
        self.assertTrue(guard.ok)
        self.assertEqual(guard.actual, "refused")
        self.assertIn("guard working as intended", guard.detail)
        task = next(c for c in report.checks if c.name == "sign-in task created and removed")
        self.assertTrue(task.skipped, "the task path is not exercised when the guard refuses")
        self.assertEqual(report.failures, [], [c.detail for c in report.failures])
        installs = [c for c in cli.calls if c[0] == "autostart" and c[1] == "install"]
        self.assertTrue(
            all("--preview" in call for call in installs),
            "a refused install must not be attempted",
        )

    def test_an_unverifiable_folder_layout_is_a_pass_and_skips(self) -> None:
        cli = FakeCli(guard="unknown")
        report = harness.Report(mode="real")
        harness.phase_autostart(cli, report, install=True, profile="quiet")
        guard = next(c for c in report.checks if c.name.startswith("the privilege guard"))
        self.assertTrue(guard.ok)
        self.assertEqual(guard.actual, "unknown")
        self.assertIn("fail closed", guard.detail)
        self.assertEqual(report.failures, [], [c.detail for c in report.failures])

    def test_autostart_anyway_overrides_the_guard_and_still_cleans_up(self) -> None:
        cli = FakeCli(guard="refused")
        report = harness.Report(mode="real")
        harness.phase_autostart(cli, report, install=True, profile="quiet", override_guard=True)
        installs = [c for c in cli.calls if c[0] == "autostart" and c[1] == "install"]
        self.assertTrue(
            any("--allow-untrusted-paths" in call for call in installs),
            "the override must be passed through explicitly",
        )
        self.assertTrue(
            any(c[1] == "remove" for c in cli.calls), "the task must still be removed"
        )
        override = next(
            c for c in report.checks if c.name.startswith("privilege guard overridden")
        )
        self.assertTrue(override.skipped)
        self.assertIn("must not be done on a machine that matters", override.detail)
        self.assertEqual(report.failures, [], [c.detail for c in report.failures])

    def test_a_preview_without_a_verdict_is_a_failure(self) -> None:
        cli = FakeCli(guard="silent")
        report = harness.Report(mode="real")
        harness.phase_autostart(cli, report, install=False, profile="quiet")
        guard = next(c for c in report.checks if c.name.startswith("the privilege guard"))
        self.assertFalse(guard.ok)
        self.assertEqual(guard.actual, "silent")

    def test_a_successful_round_trip_passes_every_check(self) -> None:
        cli = FakeCli()
        report = harness.Report(mode="real")
        harness.phase_read(cli, report)
        harness.phase_write(cli, report, "quiet")
        harness.phase_autostart(cli, report, install=False, profile="quiet")
        self.assertEqual(report.failures, [], [c.detail for c in report.failures])
        self.assertEqual(len(report.skipped), 1, "the task phase was not requested")

    def test_write_phase_records_the_expected_and_actual_values(self) -> None:
        cli = FakeCli()
        report = harness.Report(mode="real")
        harness.phase_read(cli, report)
        harness.phase_write(cli, report, "quiet")
        check = next(c for c in report.checks if c.name == "written values match the plan")
        self.assertEqual(check.expected[("max_state", "AC")], 60)
        self.assertEqual(check.actual["max_state/AC"], {"expected": 60, "actual": 60})

    def test_a_plan_with_nothing_to_do_is_reported_not_passed(self) -> None:
        cli = FakeCli()
        cli.json = lambda *arguments: {"changes": [], "unchanged": []}  # type: ignore[assignment]
        report = harness.Report(mode="real")
        harness.phase_read(cli, report)
        harness.phase_write(cli, report, "quiet")
        self.assertIn("a plan exists to verify", [c.name for c in report.failures])
        self.assertNotIn("apply", [call[0] for call in cli.calls], "nothing should be applied")


class TestEndToEndSimulation(TempDirTestCase):
    """Run the real script against the real simulated CLI."""

    def run_harness(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["QUIETGOVERNOR_STATE_DIR"] = str(self.state_dir)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "verify_windows.py"), *arguments],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=str(self.tmp_path),
        )

    def test_full_simulated_run_passes_and_writes_both_reports(self) -> None:
        result = self.run_harness(
            "--simulate", "--apply", "--autostart", "--out-dir", str(self.tmp_path / "out")
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("Simulation run", result.stdout)
        reports = sorted((self.tmp_path / "out").glob("verification-*"))
        self.assertEqual(len(reports), 2, reports)
        payload = json.loads(next(p for p in reports if p.suffix == ".json").read_text())
        self.assertEqual(payload["summary"]["failed"], 0)
        self.assertEqual(payload["mode"], "simulate")
        self.assertIsNone(payload["environment"]["elevated"])

    def test_read_only_run_skips_the_write_phase(self) -> None:
        result = self.run_harness(
            "--simulate", "--out-dir", str(self.tmp_path / "out")
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = json.loads(
            next((self.tmp_path / "out").glob("verification-*.json")).read_text()
        )
        skipped = [c for c in payload["checks"] if c["status"] == "skipped"]
        self.assertEqual(len(skipped), 2)
        # A simulated run is never presented as evidence, even when every check
        # that ran passed.
        self.assertIn("Simulation run", result.stdout)
        self.assertIn("Nothing to record", result.stdout)

    def test_it_refuses_to_run_as_a_windows_check_off_windows(self) -> None:
        if sys.platform.startswith("win"):
            self.skipTest("only meaningful off Windows")
        result = self.run_harness("--out-dir", str(self.tmp_path / "out"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Run it on the Windows PC", result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
