"""Opt-in auto-start: re-apply a profile at sign-in.

Why this is its own module: applying a profile needs administrator rights, so
the only ways to run it at sign-in are a UAC prompt or a scheduled task with
highest privileges. QuietGovernor does the second, and only when asked, because
a task that runs elevated at every logon is a persistent privileged component.

The security question that shapes this module: a scheduled task runs whatever
program it points at, with administrator rights. If that program (the Python
interpreter or the QuietGovernor package) lives in a folder the signed-in
account can write to, then anything that can write there gets administrator
code execution at the next sign-in. That is the standard Task Scheduler
privilege-escalation pattern, so auto-start is refused unless the interpreter
and the package live in administrator-only folders — with a loud, explicit
override for people who accept the risk.

Everything here except the final operating-system calls is platform-neutral and
tested, using an in-memory scheduler.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .errors import ActionRefused, BackendError, QuietGovernorError
from .store import StateStore

#: Task Scheduler location and name. No spaces: fewer quoting layers between
#: this module and schtasks, and it is still readable in Task Scheduler.
TASK_PATH = r"\QuietGovernor\ApplyAtLogon"

TASK_DESCRIPTION = (
    "QuietGovernor: re-apply a processor power profile after sign-in. "
    "Created by 'quietgovernor autostart install'; remove it with "
    "'quietgovernor autostart remove'."
)

#: How long after sign-in the task waits before running. Applying power
#: settings during the logon storm competes with everything else starting up.
LOGON_DELAY = "PT20S"

DEFAULT_PROFILE = "quiet"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --- what a task is --------------------------------------------------------


@dataclass(frozen=True)
class AutostartCheck:
    """One preflight check.

    ``severity`` is "blocking" when a failure must stop the action, or
    "warning" when it is recorded and shown but does not stop anything.
    """

    key: str
    severity: str
    passed: bool
    detail: str



@dataclass(frozen=True)
class TaskSpec:
    """The command a scheduled task would run, and where it would live."""

    task_path: str
    profile: str
    interpreter: str
    package_root: str
    state_dir: str

    @property
    def arguments(self) -> tuple[str, ...]:
        return (
            "-m",
            "quietgovernor",
            "--state-dir",
            self.state_dir,
            "autostart",
            "run",
            "--profile",
            self.profile,
        )

    def command_line(self) -> str:
        return " ".join([self.interpreter, *self.arguments])

    def to_dict(self) -> dict[str, object]:
        return {
            "task_path": self.task_path,
            "profile": self.profile,
            "interpreter": self.interpreter,
            "package_root": self.package_root,
            "state_dir": self.state_dir,
            "arguments": list(self.arguments),
        }


def task_xml(spec: TaskSpec, *, user: str) -> str:
    """Build the Task Scheduler definition for ``spec``.

    A logon trigger for ``user``, highest available privileges (power settings
    need admin), no battery/network/idle conditions (a power setting is exactly
    as relevant on battery), and a two-minute limit because applying three
    settings is a few API calls.

    The XML schema is used instead of ``schtasks`` command-line switches because
    switches cannot express "run elevated on logon" and because XML in and XML
    out is the same in every display language, unlike schtasks' text output.
    """
    arguments = " ".join(_quote(part) for part in spec.arguments)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{_escape(TASK_DESCRIPTION)}</Description>
    <URI>{_escape(spec.task_path)}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_escape(user)}</UserId>
      <Delay>{LOGON_DELAY}</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Priority>7</Priority>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_escape(spec.interpreter)}</Command>
      <Arguments>{_escape(arguments)}</Arguments>
      <WorkingDirectory>{_escape(os.path.dirname(spec.interpreter) or ".")}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _quote(part: str) -> str:
    """Quote one argument for the Windows command line."""
    if part and not any(character in part for character in ' \t"'):
        return part
    return '"' + part.replace('"', r"\"") + '"'


# --- path trust ------------------------------------------------------------

USER_WRITABLE_HELP = (
    "Auto-start needs the Python interpreter and the QuietGovernor package to "
    "live in a folder only administrators can write to. Otherwise anything that "
    "can write to that folder could run code as administrator at your next "
    "sign-in, which is the standard way scheduled tasks are abused to escalate "
    "privileges. Fix this by installing Python 'for all users' (which puts it in "
    "Program Files) and reinstalling QuietGovernor with an elevated "
    "'pip install --target \"C:\\Program Files\\QuietGovernor\" ...', or use a "
    "packaged build, which installs there already."
)


_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def looks_like_windows_path(path: str) -> bool:
    """True for ``C:\\...`` style paths, so comparisons use Windows rules."""
    return bool(_DRIVE_RE.match(path)) or "\\" in path


def _split(path: str) -> list[str]:
    """Split a path on either separator, resolving '.' and '..' lexically.

    Deliberately not ``os.path``: these checks must give the same answer on the
    machine that will host the task (Windows) and in the tests and demos that
    run elsewhere, and ``os.path`` applies the *current* platform's rules.
    """
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return parts


def _normalise(path: str) -> list[str]:
    path = os.path.expanduser(path)
    parts = _split(path)
    if looks_like_windows_path(path):
        # Windows filenames are case-insensitive.
        parts = [part.casefold() for part in parts]
    return parts


def protected_roots(env: Mapping[str, str]) -> tuple[tuple[str, ...], ...]:
    """Administrator-only folders that may hold auto-start code.

    ``ProgramData`` is deliberately excluded: its default permissions let
    ordinary users create files there.
    """
    keys = ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)", "SYSTEMROOT", "WINDIR")
    roots: list[tuple[str, ...]] = []
    for key in keys:
        value = env.get(key) or env.get(key.upper())
        if value:
            normalised = tuple(_normalise(value))
            if normalised and normalised not in roots:
                roots.append(normalised)
    return tuple(roots)


def is_in_protected_location(path: str, roots: tuple[tuple[str, ...], ...]) -> bool:
    """True when ``path`` sits inside one of the administrator-only roots."""
    if not roots or not path:
        return False
    candidate = tuple(_normalise(path))
    if not candidate:
        return False
    for root in roots:
        if len(candidate) < len(root):
            continue
        if candidate[: len(root)] == root:
            return True
    return False


# --- the operating-system boundary ----------------------------------------


class Scheduler(Protocol):
    """Create, inspect, and delete one scheduled task."""

    def create(self, spec: TaskSpec) -> None:
        """Register the task. Raises :class:`BackendError` on failure."""

    def delete(self, task_path: str) -> None:
        """Remove the task. Raises :class:`BackendError` on failure."""

    def describe(self, task_path: str) -> dict[str, str] | None:
        """Return ``command``/``arguments``/``run_level``, or None if absent."""


class SimulatedScheduler:
    """An in-memory Task Scheduler that persists so demos behave consistently."""

    def __init__(self, path: os.PathLike[str] | str | None = None) -> None:
        self.path = None if path is None else Path(os.fspath(path))
        self.tasks: dict[str, TaskSpec] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        import json

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for path, raw in payload.get("tasks", {}).items():
            if not isinstance(raw, dict):
                continue
            try:
                self.tasks[path] = TaskSpec(
                    task_path=path,
                    profile=str(raw["profile"]),
                    interpreter=str(raw["interpreter"]),
                    package_root=str(raw["package_root"]),
                    state_dir=str(raw["state_dir"]),
                )
            except KeyError:
                continue

    def _save(self) -> None:
        if self.path is None:
            return
        import json

        from .store import atomic_write_text

        payload = {"tasks": {path: spec.to_dict() for path, spec in self.tasks.items()}}
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")

    def create(self, spec: TaskSpec) -> None:
        self.tasks[spec.task_path] = spec
        self._save()

    def delete(self, task_path: str) -> None:
        if task_path not in self.tasks:
            raise BackendError(f"There is no scheduled task called {task_path}.")
        del self.tasks[task_path]
        self._save()

    def describe(self, task_path: str) -> dict[str, str] | None:
        spec = self.tasks.get(task_path)
        if spec is None:
            return None
        return {
            "command": spec.interpreter,
            "arguments": " ".join(spec.arguments),
            "run_level": "HighestAvailable",
            "profile": spec.profile,
            "package_root": spec.package_root,
            "state_dir": spec.state_dir,
        }


# --- stored record ---------------------------------------------------------


@dataclass(frozen=True)
class LastRun:
    """The outcome of the most recent auto-start run."""

    at: str
    profile: str
    ok: bool
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"at": self.at, "profile": self.profile, "ok": self.ok, "message": self.message}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "LastRun":
        return cls(
            at=str(data.get("at", "")),
            profile=str(data.get("profile", "")),
            ok=bool(data.get("ok", False)),
            message=str(data.get("message", "")),
        )


@dataclass(frozen=True)
class InstalledTask:
    """What QuietGovernor believes it registered, recorded when it did so."""

    profile: str
    interpreter: str
    package_root: str
    state_dir: str
    installed_at: str
    allow_untrusted_paths: bool = False
    last_run: LastRun | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "interpreter": self.interpreter,
            "package_root": self.package_root,
            "state_dir": self.state_dir,
            "installed_at": self.installed_at,
            "allow_untrusted_paths": self.allow_untrusted_paths,
            "last_run": self.last_run.to_dict() if self.last_run else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "InstalledTask":
        last_raw = data.get("last_run")
        return cls(
            profile=str(data.get("profile", "")),
            interpreter=str(data.get("interpreter", "")),
            package_root=str(data.get("package_root", "")),
            state_dir=str(data.get("state_dir", "")),
            installed_at=str(data.get("installed_at", "")),
            allow_untrusted_paths=bool(data.get("allow_untrusted_paths", False)),
            last_run=(
                LastRun.from_dict(last_raw)  # type: ignore[arg-type]
                if isinstance(last_raw, Mapping)
                else None
            ),
        )


# --- plans -----------------------------------------------------------------


@dataclass(frozen=True)
class AutostartPlan:
    """What ``autostart install`` would do, with checks the user can review."""

    action: str  # "install" or "remove"
    task_path: str
    profile: str
    interpreter: str
    package_root: str
    state_dir: str
    checks: tuple[AutostartCheck, ...]
    existing: dict[str, str] | None = None
    notes: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        """True when a failed check must stop the action (not just warn)."""
        return any(
            not check.passed for check in self.checks if check.severity == "blocking"
        )

    def failures(self) -> tuple[AutostartCheck, ...]:
        """Every check that did not pass, blocking or not."""
        return tuple(check for check in self.checks if not check.passed)

    def summary(self) -> str:
        if self.action == "remove":
            return f"Remove the scheduled task {self.task_path}."
        if self.existing is not None:
            return (
                f"Replace the scheduled task {self.task_path} so it applies the "
                f"'{self.profile}' profile at sign-in."
            )
        return (
            f"Create the scheduled task {self.task_path} to apply the "
            f"'{self.profile}' profile at sign-in."
        )

    def detail_lines(self) -> list[str]:
        lines = [self.summary(), ""]
        if self.action == "install":
            lines.append(f"  Runs at sign-in, {LOGON_DELAY[2:-1]} seconds later, as you, with administrator rights:")
            lines.append(f"    {self.interpreter}")
            lines.append(
                "    -m quietgovernor --state-dir "
                f"\"{self.state_dir}\" autostart run --profile {self.profile}"
            )
            lines.append("")
            lines.append("  What it does not do: no window, no tray icon, no service,")
            lines.append("  and no network access. It applies one profile and records the result.")
        failures = self.failures()
        blocking = [check for check in failures if check.severity == "blocking"]
        if blocking:
            _blank(lines)
            lines.append("Problems that stop this:")
            for check in blocking:
                lines.append(f"  ! {check.detail}")
        for check in failures:
            if check.severity == "warning":
                _blank(lines)
                lines.append("Warning:")
                lines.append(f"  ! {check.detail}")
        # For a removal, "the task exists" adds nothing the summary did not say.
        passed = [check for check in self.checks if check.passed]
        if passed and self.action == "install":
            _blank(lines)
            lines.append("Checks passed:")
            for check in passed:
                lines.append(f"  - {check.detail}")
        if self.notes:
            _blank(lines)
            for note in self.notes:
                lines.append(f"Note: {note}")
        while lines and lines[-1] == "":
            lines.pop()
        return lines


@dataclass(frozen=True)
class AutostartStatus:
    """What ``status`` reports about auto-start."""

    task_path: str
    registered: bool
    recorded: InstalledTask | None
    live: dict[str, str] | None
    #: Human sentences about anything inconsistent (stale path, other profile).
    problems: tuple[str, ...] = ()
    last_run: LastRun | None = None

    @property
    def installed(self) -> bool:
        return self.registered

    @property
    def consistent(self) -> bool:
        return self.registered and not self.problems


# --- the manager -----------------------------------------------------------


class AutostartManager:
    """Preview, install, verify, and remove the sign-in task."""

    def __init__(
        self,
        store: StateStore,
        scheduler: Scheduler,
        *,
        interpreter: str,
        package_root: str | None,
        env: Mapping[str, str],
        profile_exists: Callable[[str], bool],
        task_path: str = TASK_PATH,
        user: str = "",
        supervisor: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.scheduler = scheduler
        self.interpreter = interpreter
        self.package_root = package_root
        self.env = env
        self.profile_exists = profile_exists
        self.task_path = task_path
        self.user = user
        # Returns True when the process can create a HighestAvailable task.
        self.supervisor = supervisor

    # -- record ------------------------------------------------------------

    def load_record(self) -> InstalledTask | None:
        data = self.store.load_document("autostart")
        if data is None:
            return None
        try:
            return InstalledTask.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise QuietGovernorError(
                f"The auto-start record at {self.store.autostart_path} could not be "
                f"read ({exc}). Delete that file and run "
                "'quietgovernor autostart install' again; the scheduled task "
                "itself is unaffected."
            ) from exc

    def _spec(self, profile: str) -> TaskSpec:
        return TaskSpec(
            task_path=self.task_path,
            profile=profile,
            interpreter=self.interpreter,
            package_root=self.package_root or "",
            state_dir=str(self.store.directory),
        )

    # -- planning ----------------------------------------------------------

    def plan_install(
        self, profile: str = DEFAULT_PROFILE, *, allow_untrusted_paths: bool = False
    ) -> AutostartPlan:
        """Check everything first, so a refusal happens before any change."""
        checks: list[AutostartCheck] = []
        notes: list[str] = []

        if not self.profile_exists(profile):
            checks.append(
                AutostartCheck(
                    "profile",
                    "blocking",
                    False,
                    f"There is no profile called '{profile}'. Run "
                    "'quietgovernor profiles' to see the ones that exist, then "
                    "choose one with --profile.",
                )
            )
        else:
            checks.append(
                AutostartCheck(
                    "profile",
                    "blocking",
                    True,
                    f"The '{profile}' profile exists and can be applied.",
                )
            )

        if self.supervisor is not None:
            if self.supervisor():
                checks.append(
                    AutostartCheck(
                        "elevation",
                        "blocking",
                        True,
                        "This terminal is elevated, so Windows will accept a task "
                        "that runs with administrator rights.",
                    )
                )
            else:
                checks.append(
                    AutostartCheck(
                        "elevation",
                        "blocking",
                        False,
                        "Creating a task that runs with administrator rights needs an "
                        "elevated terminal. Right-click your terminal and choose 'Run "
                        "as administrator', then run this command again.",
                    )
                )

        roots = protected_roots(self.env)
        if self.package_root is None:
            checks.append(
                AutostartCheck(
                    "package",
                    "blocking",
                    False,
                    "QuietGovernor is not installed into this Python interpreter, so "
                    "there is nothing for the task to run. Install it with "
                    "'pip install quietgovernor' (or run from a real installation) "
                    "and try again.",
                )
            )
        else:
            untrusted = [
                path
                for path in (self.interpreter, self.package_root)
                if not is_in_protected_location(path, roots)
            ]
            if roots and not untrusted:
                checks.append(
                    AutostartCheck(
                        "package",
                        "blocking",
                        True,
                        "The interpreter and the QuietGovernor package both live in "
                        "administrator-only folders, so the task cannot be hijacked by "
                        "anything running as your normal user.",
                    )
                )
            elif allow_untrusted_paths:
                checks.append(
                    AutostartCheck(
                        "package",
                        "warning",
                        False,
                        "Auto-start will run code that is not in an administrator-only "
                        "folder: "
                        + ", ".join(untrusted or [self.interpreter])
                        + ". Anything that can write there could run code as "
                        "administrator at your next sign-in. You accepted this with "
                        "--allow-untrusted-paths.",
                    )
                )
                notes.append(
                    "This task runs code from a location QuietGovernor cannot vouch "
                    "for. Remove it with 'quietgovernor autostart remove' if you "
                    "change your mind."
                )
            elif not roots:
                # Fail closed: without known administrator-only folders, the safety
                # claim cannot be checked, and this task runs as administrator.
                checks.append(
                    AutostartCheck(
                        "package",
                        "blocking",
                        False,
                        "Could not work out which folders are administrator-only on "
                        "this system, so QuietGovernor cannot confirm the task would "
                        "not run code from a folder your account can write to. "
                        "Auto-start was not installed. Run this from a normal Windows "
                        "terminal; if you are certain the paths below are safe, "
                        "re-run with --allow-untrusted-paths.",
                    )
                )
            else:
                checks.append(
                    AutostartCheck(
                        "package",
                        "blocking",
                        False,
                        USER_WRITABLE_HELP
                        + " These paths are in a folder your account can write to: "
                        + ", ".join(untrusted)
                        + ". Nothing was changed.",
                    )
                )

        existing = self.scheduler.describe(self.task_path)
        if existing is not None:
            current = existing.get("profile") or _profile_from_arguments(
                existing.get("arguments", "")
            )
            notes.append(
                f"A task at {self.task_path} already exists"
                + (f" and applies the '{current}' profile" if current else "")
                + ". Installing replaces it; nothing else is affected."
            )

        if self.supervisor is None:
            notes.append(
                "Elevation could not be checked in this environment, so Windows will "
                "refuse the task if this terminal is not elevated."
            )

        return AutostartPlan(
            action="install",
            task_path=self.task_path,
            profile=profile,
            interpreter=self.interpreter,
            package_root=self.package_root or "",
            state_dir=str(self.store.directory),
            checks=tuple(checks),
            existing=existing,
            notes=tuple(dict.fromkeys(notes)),
        )

    def plan_remove(self) -> AutostartPlan:
        existing = self.scheduler.describe(self.task_path)
        checks = [
            AutostartCheck(
                "exists",
                "blocking",
                existing is not None,
                (
                    f"The task {self.task_path} exists and will be deleted."
                    if existing is not None
                    else f"There is no task at {self.task_path}, so there is nothing to remove."
                ),
            )
        ]
        notes: list[str] = []
        record = self.load_record()
        if existing is not None and not _is_ours(existing, self.interpreter):
            notes.append(
                "That task exists but was not created by this QuietGovernor "
                "installation (its command does not mention quietgovernor). "
                "Review it in Task Scheduler before deleting it."
            )
        if record is not None and existing is None:
            notes.append(
                "QuietGovernor has a record of installing a task, but the task is "
                "gone. Removing clears the record; no power settings are touched."
            )
        return AutostartPlan(
            action="remove",
            task_path=self.task_path,
            profile=record.profile if record else "",
            interpreter=record.interpreter if record else self.interpreter,
            package_root=record.package_root if record else (self.package_root or ""),
            state_dir=str(self.store.directory),
            checks=tuple(checks),
            existing=existing,
            notes=tuple(notes),
        )

    # -- acting ------------------------------------------------------------

    def install(
        self,
        profile: str = DEFAULT_PROFILE,
        *,
        preview: bool = False,
        confirm: Callable[[AutostartPlan], bool] | None = None,
        allow_untrusted_paths: bool = False,
    ) -> tuple[AutostartPlan, bool]:
        """Create the task. Returns (plan, changed)."""
        plan = self.plan_install(profile, allow_untrusted_paths=allow_untrusted_paths)
        if preview:
            return plan, False
        if plan.blocked:
            raise QuietGovernorError(_blocked_message(plan, "installed"))
        if confirm is not None and not confirm(plan):
            raise ActionRefused(
                "Nothing was changed: the task was not created. No power settings "
                "and no scheduled tasks were modified."
            )
        spec = self._spec(profile)
        try:
            self.scheduler.create(spec)
        except BackendError:
            raise
        # Verify by asking Task Scheduler what it now holds: a create that
        # silently did nothing would otherwise look like success.
        live = self.scheduler.describe(self.task_path)
        if live is None:
            raise BackendError(
                f"Windows reported success but there is no task at {self.task_path}. "
                "Nothing further was changed. Open Task Scheduler and check whether "
                "QuietGovernor appears under Task Scheduler Library; if it does not, "
                "this may be a permissions problem with the task folder."
            )
        if not _same_command(live, spec):
            raise BackendError(
                "The task was created but Task Scheduler reports a different command "
                "than the one requested, so it was removed again:\n"
                f"  expected: {spec.command_line()}\n"
                f"  actual:   {live.get('command', '?')} {live.get('arguments', '')}\n"
                "Your power settings were not changed."
            )
        self.store.save_document(
            "autostart",
            InstalledTask(
                profile=profile,
                interpreter=spec.interpreter,
                package_root=spec.package_root,
                state_dir=spec.state_dir,
                installed_at=utc_now(),
                allow_untrusted_paths=allow_untrusted_paths,
                last_run=None,
            ).to_dict(),
        )
        return plan, True

    def remove(
        self,
        *,
        preview: bool = False,
        confirm: Callable[[AutostartPlan], bool] | None = None,
    ) -> tuple[AutostartPlan, bool]:
        """Delete the task. Returns (plan, changed)."""
        plan = self.plan_remove()
        if preview:
            return plan, False
        if plan.blocked:
            # Nothing to remove; clear any stale record so status stops lying.
            if self.load_record() is not None:
                self.store.clear_document("autostart")
            raise QuietGovernorError(
                f"There is no task at {self.task_path}, so there was nothing to "
                "remove. Any stale auto-start record was cleared. Your power "
                "settings were not touched."
            )
        if confirm is not None and not confirm(plan):
            raise ActionRefused(
                "Nothing was changed: the task was left in place."
            )
        self.scheduler.delete(self.task_path)
        if self.scheduler.describe(self.task_path) is not None:
            raise BackendError(
                f"Windows reported success but the task {self.task_path} is still "
                "registered. Remove it manually in Task Scheduler. No power "
                "settings were touched."
            )
        self.store.clear_document("autostart")
        return plan, True

    # -- the task's own payload -------------------------------------------

    def record_run(self, profile: str, ok: bool, message: str = "") -> None:
        """Save how the most recent auto-start run went, for ``status``."""
        record = self.load_record()
        if record is None:
            return
        updated = InstalledTask(
            profile=record.profile,
            interpreter=record.interpreter,
            package_root=record.package_root,
            state_dir=record.state_dir,
            installed_at=record.installed_at,
            allow_untrusted_paths=record.allow_untrusted_paths,
            last_run=LastRun(at=utc_now(), profile=profile, ok=ok, message=message[:500]),
        )
        self.store.save_document("autostart", updated.to_dict())

    # -- reporting ---------------------------------------------------------

    def status(self) -> AutostartStatus:
        live = self.scheduler.describe(self.task_path)
        record = self.load_record()
        problems: list[str] = []
        if live is not None and record is None:
            problems.append(
                f"A task at {self.task_path} exists but QuietGovernor has no record "
                "of installing it. It may be left over from another installation or "
                "created by hand."
            )
        if record is not None and live is None:
            problems.append(
                f"QuietGovernor recorded an auto-start task, but {self.task_path} is "
                "no longer registered. Run 'quietgovernor autostart install' to "
                "recreate it, or 'quietgovernor autostart remove' to clear the record."
            )
        if live is not None and record is not None:
            if not _is_ours(live, self.interpreter):
                problems.append(
                    "The registered task does not point at QuietGovernor, so it was "
                    "probably not created by this installation."
                )
            recorded_arguments = " ".join(
                self._spec(record.profile).arguments
            )
            if live.get("arguments") and live["arguments"] != recorded_arguments:
                problems.append(
                    "The registered task's command does not match what QuietGovernor "
                    "recorded, so it may apply a different profile or interpreter. "
                    "Reinstall with 'quietgovernor autostart install --profile NAME'."
                )
        return AutostartStatus(
            task_path=self.task_path,
            registered=live is not None,
            recorded=record,
            live=live,
            problems=tuple(problems),
            last_run=record.last_run if record else None,
        )


def _blank(lines: list[str]) -> None:
    """Insert a separator line unless one is already there."""
    if lines and lines[-1] != "":
        lines.append("")


def _blocked_message(plan: AutostartPlan, verb: str) -> str:
    """Turn the blocking checks into one message that carries the fix path.

    The caller may be a script using --json, where the plan was never printed,
    so the reason has to live in the error itself.
    """
    reasons = [check.detail for check in plan.failures() if check.severity == "blocking"]
    return (
        f"Auto-start was not {verb}, and nothing on this PC was changed. "
        + " ".join(reasons)
    )


def _profile_from_arguments(arguments: str) -> str:
    parts = arguments.split()
    for index, part in enumerate(parts):
        if part == "--profile" and index + 1 < len(parts):
            return parts[index + 1].strip('"')
    return ""


def _is_ours(live: Mapping[str, str], interpreter: str) -> bool:
    """True when the registered task points at *this* interpreter.

    A substring test on the arguments would match any task that mentions the
    word 'quietgovernor'; comparing the command path is what actually
    distinguishes this installation's task from someone else's.
    """
    command = (live.get("command") or "").strip()
    if not command or not interpreter:
        return False
    return _normalise(command) == _normalise(interpreter)


def _same_command(live: Mapping[str, str], spec: TaskSpec) -> bool:
    command = (live.get("command") or "").strip()
    arguments = (live.get("arguments") or "").strip()
    if command and os.path.normcase(command) != os.path.normcase(spec.interpreter):
        return False
    if arguments:
        expected = " ".join(_quote(part) for part in spec.arguments)
        if arguments != expected and arguments != " ".join(spec.arguments):
            return False
    return True
