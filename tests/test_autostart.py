"""Auto-start: preflight checks, install/remove/verify, and the task payload."""

from __future__ import annotations

import unittest

from tests import TempDirTestCase

from quietgovernor.autostart import (
    TASK_PATH,
    AutostartManager,
    SimulatedScheduler,
    TaskSpec,
    is_in_protected_location,
    protected_roots,
    task_xml,
)
from quietgovernor.backend import SimulatedBackend
from quietgovernor.controller import Controller
from quietgovernor.errors import ActionRefused, BackendError, QuietGovernorError
from quietgovernor.policy import AC, DC
from quietgovernor.store import StateStore

WINDOWS_ENV = {
    "PROGRAMFILES": r"C:\Program Files",
    "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
    "PROGRAMW6432": r"C:\Program Files",
    "SYSTEMROOT": r"C:\Windows",
}
SAFE_INTERPRETER = r"C:\Program Files\Python312\pythonw.exe"
SAFE_PACKAGE = r"C:\Program Files\Python312\Lib\site-packages\quietgovernor"
USER_INTERPRETER = r"C:\Users\sean\AppData\Local\Programs\Python\Python312\python.exe"
USER_PACKAGE = (
    r"C:\Users\sean\AppData\Local\Programs\Python\Python312"
    r"\Lib\site-packages\quietgovernor"
)


class TestProtectedPaths(unittest.TestCase):
    def test_roots_come_from_the_environment(self) -> None:
        roots = protected_roots(WINDOWS_ENV)
        self.assertIn(("c:", "program files"), roots)
        self.assertIn(("c:", "program files (x86)"), roots)
        self.assertIn(("c:", "windows"), roots)
        # Duplicates collapse: PROGRAMFILES and PROGRAMW6432 are the same folder.
        self.assertEqual(len(roots), 3)

    def test_admins_only_folders_are_trusted(self) -> None:
        roots = protected_roots(WINDOWS_ENV)
        for path in (
            SAFE_INTERPRETER,
            SAFE_PACKAGE,
            r"C:\Windows\System32\cmd.exe",
            r"C:\Program Files (x86)\SomeApp\app.exe",
            r"c:\program files\python312\pythonw.exe",  # Windows is case-insensitive
            r"C:\Program Files\..\Program Files\x.exe",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_in_protected_location(path, roots))

    def test_user_writable_folders_are_not_trusted(self) -> None:
        roots = protected_roots(WINDOWS_ENV)
        for path in (
            USER_INTERPRETER,
            USER_PACKAGE,
            r"C:\ProgramData\QuietGovernor\x.exe",
            "/usr/bin/python3",
            "",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_in_protected_location(path, roots))

    def test_a_parent_of_a_root_is_not_trusted(self) -> None:
        roots = protected_roots(WINDOWS_ENV)
        self.assertFalse(is_in_protected_location(r"C:\Program", roots))

    def test_no_roots_means_nothing_is_trusted(self) -> None:
        self.assertEqual(protected_roots({}), ())
        self.assertFalse(is_in_protected_location(SAFE_INTERPRETER, ()))


class TaskXmlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = TaskSpec(
            task_path=TASK_PATH,
            profile="quiet",
            interpreter=SAFE_INTERPRETER,
            package_root=SAFE_PACKAGE,
            state_dir=r"C:\Users\sean\AppData\Local\QuietGovernor",
        )

    def test_runs_elevated_at_logon_for_the_user(self) -> None:
        xml = task_xml(self.spec, user=r"LAPTOP\sean")
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<UserId>LAPTOP\\sean</UserId>", xml)
        self.assertIn("<Delay>PT20S</Delay>", xml)

    def test_uses_the_long_form_autostart_run_command(self) -> None:
        xml = task_xml(self.spec, user=r"LAPTOP\sean")
        self.assertIn("<Command>" + SAFE_INTERPRETER.replace("\\", "\\") + "</Command>", xml)
        self.assertIn("autostart", xml)
        self.assertIn("--profile quiet", xml)
        self.assertIn("--state-dir", xml)

    def test_ignores_battery_and_network_conditions(self) -> None:
        xml = task_xml(self.spec, user=r"LAPTOP\sean")
        self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", xml)
        self.assertIn("<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", xml)
        self.assertIn("<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>", xml)
        self.assertIn("<RunOnlyIfIdle>false</RunOnlyIfIdle>", xml)

    def test_is_wellformed_and_has_a_bounded_run_time(self) -> None:
        import xml.etree.ElementTree as ElementTree

        root = ElementTree.fromstring(task_xml(self.spec, user=r"LAPTOP\sean"))
        self.assertTrue(root.tag.endswith("Task"))
        self.assertIn("<ExecutionTimeLimit>PT2M</ExecutionTimeLimit>", task_xml(self.spec, user="u"))

    def test_arguments_are_quoted_and_escaped(self) -> None:
        spec = TaskSpec(
            task_path=TASK_PATH,
            profile="quiet",
            interpreter=SAFE_INTERPRETER,
            package_root=SAFE_PACKAGE,
            state_dir=r"C:\Program Files\Quiet Governor\state",
        )
        xml = task_xml(spec, user="u")
        self.assertIn("&quot;C:\\Program Files\\Quiet Governor\\state&quot;", xml)

    def test_ampersands_in_a_user_name_are_escaped(self) -> None:
        xml = task_xml(self.spec, user="Big & Co\\sean")
        self.assertIn("Big &amp; Co\\sean", xml)


class AutostartCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = StateStore(self.state_dir)
        self.backend = SimulatedBackend()
        self.controller = Controller(self.backend, self.store)
        self.scheduler = SimulatedScheduler()

    def manager(
        self,
        *,
        interpreter: str = SAFE_INTERPRETER,
        package_root: str | None = SAFE_PACKAGE,
        env: dict[str, str] | None = None,
        supervisor=lambda: True,
        existing_profiles=("quiet", "balanced"),
    ) -> AutostartManager:
        return AutostartManager(
            self.store,
            self.scheduler,
            interpreter=interpreter,
            package_root=package_root,
            env=WINDOWS_ENV if env is None else env,
            profile_exists=lambda name: name in existing_profiles,
            supervisor=supervisor,
            user=r"LAPTOP\sean",
        )


class TestInstall(AutostartCase):
    def test_install_creates_the_task_and_records_it(self) -> None:
        manager = self.manager()
        plan, changed = manager.install("quiet", confirm=lambda plan: True)
        self.assertTrue(changed)
        self.assertIn(TASK_PATH, self.scheduler.tasks)
        record = self.store.load_document("autostart")
        assert record is not None
        self.assertEqual(record["profile"], "quiet")
        self.assertFalse(record["allow_untrusted_paths"])

    def test_preview_creates_nothing(self) -> None:
        manager = self.manager()
        plan, changed = manager.install("quiet", preview=True)
        self.assertFalse(changed)
        self.assertEqual(self.scheduler.tasks, {})
        self.assertIsNone(self.store.load_document("autostart"))

    def test_declining_changes_nothing(self) -> None:
        manager = self.manager()
        with self.assertRaises(ActionRefused):
            manager.install("quiet", confirm=lambda plan: False)
        self.assertEqual(self.scheduler.tasks, {})
        self.assertIsNone(self.store.load_document("autostart"))

    def test_unknown_profile_is_blocked_before_anything_happens(self) -> None:
        manager = self.manager()
        plan = manager.plan_install("nonexistent")
        self.assertTrue(plan.blocked)
        self.assertIn("no profile called", " ".join(c.detail for c in plan.failures()))
        with self.assertRaises(QuietGovernorError):
            manager.install("nonexistent", confirm=lambda plan: True)
        self.assertEqual(self.scheduler.tasks, {})

    def test_unelevated_terminal_is_blocked(self) -> None:
        manager = self.manager(supervisor=lambda: False)
        plan = manager.plan_install("quiet")
        self.assertTrue(plan.blocked)
        self.assertIn("Run as administrator", " ".join(c.detail for c in plan.failures()))

    def test_user_writable_code_is_blocked_with_the_reason(self) -> None:
        manager = self.manager(interpreter=USER_INTERPRETER, package_root=USER_PACKAGE)
        plan = manager.plan_install("quiet")
        self.assertTrue(plan.blocked)
        detail = " ".join(check.detail for check in plan.failures())
        self.assertIn("escalate privileges", detail)
        self.assertIn(USER_INTERPRETER, detail)
        self.assertIn("Nothing was changed", detail)

    def test_user_writable_code_can_be_overridden_loudly(self) -> None:
        manager = self.manager(interpreter=USER_INTERPRETER, package_root=USER_PACKAGE)
        plan = manager.plan_install("quiet", allow_untrusted_paths=True)
        self.assertFalse(plan.blocked, "an explicit override should not be blocked")
        warnings = [check for check in plan.failures() if check.severity == "warning"]
        self.assertTrue(warnings, "the override must still be reported")
        self.assertIn("not in an administrator-only folder", warnings[0].detail)
        self.assertIn("run code as administrator", warnings[0].detail)
        _plan, changed = manager.install(
            "quiet", confirm=lambda plan: True, allow_untrusted_paths=True
        )
        self.assertTrue(changed)
        record = self.store.load_document("autostart")
        assert record is not None
        self.assertTrue(record["allow_untrusted_paths"])

    def test_missing_package_is_blocked(self) -> None:
        manager = self.manager(package_root=None)
        plan = manager.plan_install("quiet")
        self.assertTrue(plan.blocked)
        self.assertIn("not installed into this Python interpreter", " ".join(
            check.detail for check in plan.failures()
        ))

    def test_unknown_folder_layout_fails_closed(self) -> None:
        # If the administrator-only folders cannot be identified, the safety
        # claim cannot be checked, and the task runs as administrator: refuse.
        manager = self.manager(env={})
        plan = manager.plan_install("quiet")
        self.assertTrue(plan.blocked)
        detail = " ".join(check.detail for check in plan.failures())
        self.assertIn("could not work out", detail.lower())
        self.assertIn("--allow-untrusted-paths", detail)
        with self.assertRaises(QuietGovernorError):
            manager.install("quiet", confirm=lambda plan: True)
        self.assertEqual(self.scheduler.tasks, {})

    def test_unknown_folder_layout_can_be_overridden(self) -> None:
        manager = self.manager(env={})
        plan, changed = manager.install(
            "quiet", confirm=lambda plan: True, allow_untrusted_paths=True
        )
        self.assertTrue(changed)
        self.assertFalse(plan.blocked)

    def test_reinstall_replaces_the_task_and_says_so(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        plan, changed = manager.install("balanced", confirm=lambda plan: True)
        self.assertTrue(changed)
        self.assertIn("already exists", " ".join(plan.notes))
        self.assertEqual(self.scheduler.tasks[TASK_PATH].profile, "balanced")

    def test_verification_rejects_a_task_windows_did_not_create(self) -> None:
        class ForgetfulScheduler(SimulatedScheduler):
            def create(self, spec):  # accepts but does nothing
                return None

        manager = AutostartManager(
            self.store,
            ForgetfulScheduler(),
            interpreter=SAFE_INTERPRETER,
            package_root=SAFE_PACKAGE,
            env=WINDOWS_ENV,
            profile_exists=lambda name: True,
            supervisor=lambda: True,
            user=r"LAPTOP\sean",
        )
        with self.assertRaises(BackendError) as caught:
            manager.install("quiet", confirm=lambda plan: True)
        self.assertIn("no task at", str(caught.exception))
        self.assertIsNone(self.store.load_document("autostart"))

    def test_verification_rejects_a_task_with_the_wrong_command(self) -> None:
        class TamperingScheduler(SimulatedScheduler):
            def create(self, spec):
                super().create(
                    TaskSpec(
                        task_path=spec.task_path,
                        profile="balanced",
                        interpreter=SAFE_INTERPRETER,
                        package_root=SAFE_PACKAGE,
                        state_dir=spec.state_dir,
                    )
                )

        manager = AutostartManager(
            self.store,
            TamperingScheduler(),
            interpreter=SAFE_INTERPRETER,
            package_root=SAFE_PACKAGE,
            env=WINDOWS_ENV,
            profile_exists=lambda name: True,
            supervisor=lambda: True,
            user=r"LAPTOP\sean",
        )
        with self.assertRaises(BackendError) as caught:
            manager.install("quiet", confirm=lambda plan: True)
        message = str(caught.exception)
        self.assertIn("different command", message)
        self.assertIn("power settings were not changed", message)

    def test_task_arguments_carry_the_state_directory(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        spec = self.scheduler.tasks[TASK_PATH]
        self.assertEqual(spec.state_dir, str(self.store.directory))
        self.assertIn("--state-dir", spec.arguments)


class TestRemove(AutostartCase):
    def test_remove_deletes_the_task_and_the_record(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        plan, changed = manager.remove(confirm=lambda plan: True)
        self.assertTrue(changed)
        self.assertEqual(self.scheduler.tasks, {})
        self.assertIsNone(self.store.load_document("autostart"))

    def test_remove_preview_keeps_the_task(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        _plan, changed = manager.remove(preview=True)
        self.assertFalse(changed)
        self.assertIn(TASK_PATH, self.scheduler.tasks)

    def test_remove_when_absent_explains_and_clears_stale_record(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        self.scheduler.tasks.clear()  # task vanished (e.g. removed by an admin)
        with self.assertRaises(QuietGovernorError) as caught:
            manager.remove(confirm=lambda plan: True)
        self.assertIn("nothing to remove", str(caught.exception))
        self.assertIsNone(self.store.load_document("autostart"))

    def test_declining_remove_keeps_the_task(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        with self.assertRaises(ActionRefused):
            manager.remove(confirm=lambda plan: False)
        self.assertIn(TASK_PATH, self.scheduler.tasks)

    def test_remove_warns_about_a_task_it_did_not_create(self) -> None:
        self.scheduler.create(
            TaskSpec(
                task_path=TASK_PATH,
                profile="quiet",
                interpreter=r"C:\Program Files\Other\other.exe",
                package_root=r"C:\Program Files\Other",
                state_dir=r"C:\temp",
            )
        )
        # SimulatedScheduler describes it, but _is_ours looks for 'quietgovernor'.
        manager = self.manager()
        plan = manager.plan_remove()
        self.assertIn("not created by this QuietGovernor", " ".join(plan.notes))

    def test_removing_does_not_change_power_settings(self) -> None:
        manager = self.manager()
        self.controller.apply("quiet", confirm=lambda plan: True)
        before = self.backend.effective_values()
        manager.install("balanced", confirm=lambda plan: True)
        manager.remove(confirm=lambda plan: True)
        self.assertEqual(before, self.backend.effective_values())


class TestStatus(AutostartCase):
    def test_not_installed(self) -> None:
        status = self.manager().status()
        self.assertFalse(status.installed)
        self.assertIsNone(status.recorded)
        self.assertEqual(status.problems, ())

    def test_installed_and_consistent(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        status = manager.status()
        self.assertTrue(status.installed)
        self.assertTrue(status.consistent)
        assert status.recorded is not None
        self.assertEqual(status.recorded.profile, "quiet")
        self.assertEqual(status.live["run_level"], "HighestAvailable")  # type: ignore[index]

    def test_task_without_a_record_is_reported(self) -> None:
        self.scheduler.create(
            TaskSpec(
                task_path=TASK_PATH,
                profile="quiet",
                interpreter=SAFE_INTERPRETER,
                package_root=SAFE_PACKAGE,
                state_dir=str(self.store.directory),
            )
        )
        status = self.manager().status()
        self.assertTrue(status.installed)
        self.assertFalse(status.consistent)
        self.assertIn("no record", " ".join(status.problems))

    def test_record_without_a_task_is_reported(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        self.scheduler.tasks.clear()
        status = manager.status()
        self.assertFalse(status.installed)
        self.assertTrue(status.problems)
        self.assertIn("no longer registered", " ".join(status.problems))

    def test_a_swapped_task_is_reported(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        self.scheduler.create(
            TaskSpec(
                task_path=TASK_PATH,
                profile="quiet",
                interpreter=SAFE_INTERPRETER,
                package_root=SAFE_PACKAGE,
                state_dir=r"C:\somewhere\else",
            )
        )
        status = manager.status()
        self.assertFalse(status.consistent)
        self.assertIn("does not match", " ".join(status.problems))


class TestLastRun(AutostartCase):
    def test_run_outcome_is_recorded_and_shown(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        manager.record_run("quiet", True, "Applied 6 setting(s).")
        status = manager.status()
        assert status.last_run is not None
        self.assertTrue(status.last_run.ok)
        self.assertEqual(status.last_run.profile, "quiet")
        self.assertIn("Applied 6 setting(s).", status.last_run.message)

    def test_failure_is_recorded(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        manager.record_run("quiet", False, "access denied")
        status = manager.status()
        assert status.last_run is not None
        self.assertFalse(status.last_run.ok)
        self.assertIn("access denied", status.last_run.message)

    def test_recording_without_an_install_is_harmless(self) -> None:
        self.manager().record_run("quiet", True, "nothing to attach this to")
        self.assertIsNone(self.store.load_document("autostart"))

    def test_reinstall_clears_the_previous_run(self) -> None:
        manager = self.manager()
        manager.install("quiet", confirm=lambda plan: True)
        manager.record_run("quiet", True, "old run")
        manager.install("balanced", confirm=lambda plan: True)
        self.assertIsNone(manager.status().last_run)


class TestSchedulerPersistence(TempDirTestCase):
    def test_simulated_scheduler_remembers_tasks(self) -> None:
        path = self.state_dir / "tasks.json"
        first = SimulatedScheduler(path)
        first.create(
            TaskSpec(
                task_path=TASK_PATH,
                profile="quiet",
                interpreter=SAFE_INTERPRETER,
                package_root=SAFE_PACKAGE,
                state_dir=str(self.state_dir),
            )
        )
        second = SimulatedScheduler(path)
        described = second.describe(TASK_PATH)
        assert described is not None
        self.assertEqual(described["profile"], "quiet")

    def test_deleting_a_missing_task_is_an_error(self) -> None:
        with self.assertRaises(BackendError):
            SimulatedScheduler().delete(TASK_PATH)

    def test_damaged_simulation_file_is_ignored(self) -> None:
        path = self.state_dir / "tasks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(SimulatedScheduler(path).tasks, {})


class TestTaskPayload(AutostartCase):
    def test_apply_without_a_question_is_what_the_task_runs(self) -> None:
        # The consent for an unattended run was given at install time, so the
        # payload applies without a confirmer and must still verify.
        result = self.controller.apply("quiet")
        self.assertTrue(result.changed)
        self.assertTrue(result.verified)
        self.assertEqual(
            self.backend.effective_values()["max_state"], {AC: 60, DC: 60}
        )

    def test_payload_records_a_baseline_on_first_use(self) -> None:
        self.controller.apply("quiet")
        self.assertIsNotNone(self.store.load_baseline())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
