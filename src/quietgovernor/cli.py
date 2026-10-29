"""The ``quietgovernor`` command line.

Design rules that show up in the code below:

* Every command that changes something prints the plan first and asks.
  ``--yes`` skips the question but still prints the plan.
* Without a terminal (a script, a service), nothing is changed unless
  ``--yes`` is passed, so an unattended run can never alter a PC by accident.
* Errors are sentences with a fix path, printed to stderr, exit code 1.
* ``--simulate`` exercises the real policy, planning, storage, and restore
  code against an in-memory PC. It is the supported way to try QuietGovernor
  on a machine that is not Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from . import __version__
from .autostart import (
    DEFAULT_PROFILE as DEFAULT_AUTOSTART_PROFILE,
    TASK_PATH,
    AutostartManager,
    AutostartPlan,
    AutostartStatus,
    SimulatedScheduler,
)
from .backend import SimulatedBackend
from .controller import ApplyResult, Controller, Status
from .errors import ActionRefused, QuietGovernorError
from .planning import Plan
from .policy import AC, DC, SETTINGS, WRITE_ORDER, validate_scope
from .store import StateStore, default_state_dir

BOOST_NAMES = {
    "off": 0,
    "disabled": 0,
    "on": 1,
    "enabled": 1,
    "aggressive": 2,
    "efficient": 3,
    "aggressive-guaranteed": 4,
    "efficient-guaranteed": 5,
}

PROGRAM = "quietgovernor"


# --- terminal output -------------------------------------------------------


class Style:
    """Minimal ANSI styling, disabled for pipes and NO_COLOR."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def heading(self, text: str) -> str:
        return self._wrap("1;31", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def warn(self, text: str) -> str:
        return self._wrap("1;33", text)


def make_style(args: argparse.Namespace) -> Style:
    if getattr(args, "json", False):
        return Style(False)
    if getattr(args, "no_color", False) or os.environ.get("NO_COLOR"):
        return Style(False)
    return Style(sys.stdout.isatty())


def _source_label(source: str) -> str:
    return "plugged-in" if source == AC else "battery"


def _format_value(setting_key: str, value: int | None) -> str:
    if value is None:
        return "not reported"
    setting = SETTINGS[setting_key]
    if setting_key == "boost_mode":
        names = {0: "off", 1: "on", 2: "aggressive", 3: "efficient",
                 4: "aggressive-guaranteed", 5: "efficient-guaranteed"}
        return f"{names.get(value, value)}"
    return f"{value}{setting.unit}"


def print_plan(plan: Plan, style: Style, stream=None) -> None:
    # Resolve sys.stdout at call time: a default of sys.stdout would bind the
    # stream at import and defeat redirection (and capsys-style test harnesses).
    if stream is None:
        stream = sys.stdout
    print(style.heading("Plan"), file=stream)
    for line in plan.detail_lines():
        if line.startswith("  ! "):
            print("  " + style.warn("! " + line[4:]), file=stream)
        else:
            print(line, file=stream)


def print_result(
    result: ApplyResult, style: Style, stream=None, *, undo_hint: bool = True
) -> None:
    if stream is None:
        stream = sys.stdout
    if result.dry_run:
        print(
            style.dim(
                "Preview only: no settings were changed and no restore point "
                "was recorded."
            ),
            file=stream,
        )
        return
    if not result.changed:
        print("Nothing to write: this PC already matches the profile.", file=stream)
        return
    print("", file=stream)
    print(style.bold(f"Changed {len(result.applied)} setting(s) and read each one back to confirm."), file=stream)
    if result.baseline_created:
        print(
            "Restore point recorded before the first change: "
            f"{result.baseline_path}",
            file=stream,
        )
    else:
        print(
            f"Your existing restore point at {result.baseline_path} was kept "
            "unchanged.",
            file=stream,
        )
    if undo_hint:
        print(
            "Undo any time with: "
            + style.bold(f"{PROGRAM} restore")
            + " (preview it with --dry-run)",
            file=stream,
        )


# --- backend selection -----------------------------------------------------


def build_backend(args: argparse.Namespace, store: StateStore):
    if getattr(args, "simulate", False):
        return SimulatedBackend(state_path=store.directory / "simulated-machine.json")
    from .windows import WindowsBackend

    return WindowsBackend()


#: Paths used by --simulate, so the auto-start flow can be demonstrated on any
#: operating system. They are never used in real mode.
SIMULATED_ENV = {
    "PROGRAMFILES": r"C:\Program Files",
    "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
    "SYSTEMROOT": r"C:\Windows",
}
SIMULATED_INTERPRETER = r"C:\Program Files\Python312\pythonw.exe"
SIMULATED_PACKAGE_ROOT = r"C:\Program Files\Python312\Lib\site-packages\quietgovernor"
def build_autostart(
    args: argparse.Namespace, controller: Controller
) -> AutostartManager | None:
    """Assemble the auto-start manager, or None when this PC cannot host one.

    Returning None keeps a missing Task Scheduler from taking down the whole
    ``status`` command: being unable to install auto-start is not a reason to
    stop reporting power settings.
    """
    store = controller.store
    profiles = controller.profiles()
    if getattr(args, "simulate", False):
        return AutostartManager(
            store,
            SimulatedScheduler(store.directory / "simulated-tasks.json"),
            interpreter=SIMULATED_INTERPRETER,
            package_root=SIMULATED_PACKAGE_ROOT,
            env=SIMULATED_ENV,
            profile_exists=lambda name: name in profiles,
            supervisor=lambda: True,
            user="SIMULATED\\user",
        )
    from .errors import BackendError
    from .windows import (
        WindowsTaskScheduler,
        is_elevated,
        package_root,
        python_for_tasks,
    )

    try:
        scheduler = WindowsTaskScheduler()
    except BackendError:
        return None

    return AutostartManager(
        store,
        scheduler,
        interpreter=python_for_tasks(),
        package_root=package_root(),
        env=dict(os.environ),
        profile_exists=lambda name: name in profiles,
        supervisor=is_elevated,
    )


def require_autostart(args: argparse.Namespace, controller: Controller) -> AutostartManager:
    """The manager, or a clear error when this PC cannot run scheduled tasks."""
    manager = build_autostart(args, controller)
    if manager is None:
        from .errors import BackendError

        try:
            from .windows import WindowsTaskScheduler

            WindowsTaskScheduler()  # raises with the specific reason
        except BackendError as error:
            raise QuietGovernorError(
                f"Auto-start cannot be managed on this PC: {error}"
            ) from error
        raise QuietGovernorError(
            "Auto-start cannot be managed on this PC: Windows Task Scheduler is "
            "unavailable. Your power settings are unaffected."
        )
    return manager


def build_store(args: argparse.Namespace) -> StateStore:
    explicit = getattr(args, "state_dir", None)
    if getattr(args, "simulate", False) and not explicit:
        # Keep simulated state away from real state so a demo can never make
        # 'status' on the real machine look governed.
        return StateStore(default_state_dir() / "simulated")
    return StateStore(explicit)


def build_controller(args: argparse.Namespace) -> Controller:
    store = build_store(args)
    return Controller(build_backend(args, store), store)


def simulated_banner(args: argparse.Namespace, style: Style) -> None:
    if getattr(args, "simulate", False) and not getattr(args, "json", False):
        print(
            style.dim(
                "Simulation mode: an in-memory PC stands in for Windows, so no "
                "real power settings are read or changed. The simulated PC and "
                f"its state live in {build_store(args).directory}."
            )
        )


# --- commands --------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    style = make_style(args)
    simulated_banner(args, style)
    controller = build_controller(args)
    status = controller.status()

    if args.json:
        payload = _status_dict(status, simulated=bool(args.simulate))
        manager = build_autostart(args, controller)
        payload["autostart"] = (
            _autostart_status_dict(manager.status())
            if manager is not None
            else {
                "available": False,
                "reason": "Windows Task Scheduler is not available on this system.",
                "installed": False,
            }
        )
        print(json.dumps(payload, indent=2))
        return 0

    print(style.heading("Power plan"))
    print(f"  Active plan: {status.scheme_name}")
    print(f"  Identifier:  {status.scheme_guid}")
    print("")
    print(style.heading("Processor settings"))
    for key in WRITE_ORDER:
        reading = status.readings.get(key)
        row = [f"  {SETTINGS[key].label}"]
        for source in (AC, DC):
            value = reading.value_for(source) if reading else None
            row.append(f"{_source_label(source)}: {_format_value(key, value)}")
        print("  ".join(row))
        if reading is None or not reading.available:
            print(f"    {style.dim('this PC does not expose this setting')}")
    print("")
    print(style.heading("QuietGovernor state"))
    if status.governing and status.state is not None:
        print(f"  Managing: {status.state.profile_name} profile")
        if status.state.applied_at:
            print(f"  Applied:  {status.state.applied_at}")
        if status.plan_changed:
            print(
                "  "
                + style.warn(
                    "The active power plan is not the one this profile was applied "
                    "to. Windows keeps these settings per plan, so the numbers "
                    "below come from a different plan and do not mean the profile "
                    "was overridden."
                )
            )
            print(
                f"    Applied to: {status.state.scheme_name or 'an unnamed plan'} "
                f"({status.state.scheme_guid})"
            )
            print(f"    Active now: {status.scheme_name} ({status.scheme_guid})")
            print(
                f"    To govern the plan you are using now: {PROGRAM} apply "
                f"--profile {status.state.profile_name}"
            )
        if status.drift and not status.plan_changed:
            print(
                "  "
                + style.warn(
                    f"{len(status.drift)} managed setting(s) no longer match — "
                    "something else changed them:"
                )
            )
            for key, source, live, managed_value in status.drift:
                print(
                    f"    - {SETTINGS[key].label} on {_source_label(source)} power "
                    f"is {_format_value(key, live)}, expected "
                    f"{_format_value(key, managed_value)}"
                )
            print(
                "    Re-apply the profile to restore it, or run "
                f"'{PROGRAM} restore' to go back to your original settings."
            )
        elif not status.plan_changed:
            print("  Every managed setting matches what was applied.")
    else:
        print("  Not managing this PC: no profile is applied.")
    print("")
    print(style.heading("Restore point"))
    if status.baseline is None:
        print(
            "  None yet. QuietGovernor records your original settings before "
            "it changes anything for the first time."
        )
    else:
        print(f"  Recorded: {status.baseline.captured_at or 'unknown time'}")
        print(f"  From plan: {status.baseline.scheme_name or 'unknown plan'}")
        print(
            "  Note: the restore point is kept even after 'restore', so you can "
            "always put these values back."
        )
    print("")
    print(style.heading("Sign-in auto-start (optional)"))
    for line in _autostart_status_lines(args, controller, style):
        print(line)
    if not args.simulate:
        print("")
        print(style.dim(f"State folder: {build_store(args).directory}"))
        print(style.dim(f"Elevated: {'yes' if _elevated() else 'no'}"
                        " (changing settings needs administrator rights)"))
    return 0


def _autostart_status_lines(
    args: argparse.Namespace, controller: Controller, style: Style
) -> list[str]:
    manager = build_autostart(args, controller)
    if manager is None:
        return [
            "  Unavailable on this system: it needs Windows Task Scheduler.",
            "  Power settings are unaffected.",
        ]
    status = manager.status()
    lines: list[str] = []
    if not status.installed:
        if status.recorded is not None:
            lines.append("  Not installed, though a record of one exists (see below).")
        else:
            lines.append("  Not installed. QuietGovernor changes nothing at sign-in.")
            lines.append(
                f"  To re-apply a profile automatically: {PROGRAM} autostart install --profile quiet"
            )
        for problem in status.problems:
            lines.append("  " + style.warn(problem))
        return lines
    profile = (
        status.recorded.profile
        if status.recorded
        else (status.live or {}).get("profile", "an unknown profile")
    )
    lines.append(f"  Installed: applies the '{profile}' profile at sign-in.")
    lines.append(f"  Task:      {status.task_path}")
    if status.live is not None:
        command = status.live.get("command") or "?"
        arguments = status.live.get("arguments") or ""
        lines.append(f"  Runs:      {command} {arguments}".rstrip())
        if status.live.get("run_level"):
            lines.append(
                f"  Privileges: {status.live['run_level']} (a task that needs this "
                "much access is why installing it is a deliberate step)"
            )
    if status.last_run is not None:
        outcome = "succeeded" if status.last_run.ok else "failed"
        lines.append(f"  Last run:  {outcome} at {status.last_run.at or 'an unknown time'}")
        if status.last_run.message:
            lines.append(f"             {status.last_run.message}")
    else:
        lines.append("  Last run:  none yet (it runs shortly after you next sign in).")
    for problem in status.problems:
        lines.append("  " + style.warn(problem))
    lines.append(
        f"  Remove it any time with: {PROGRAM} autostart remove"
    )
    return lines


def _elevated() -> bool:
    try:
        from .windows import is_elevated
    except Exception:  # pragma: no cover - import trouble
        return False
    return is_elevated()


def _status_dict(status: Status, *, simulated: bool = False) -> dict:
    return {
        "scheme": {"guid": status.scheme_guid, "name": status.scheme_name},
        # Recorded so a verification report can state whether writes were even
        # possible. None means "not applicable", not "no".
        "elevated": None if simulated else _elevated(),
        "governing": status.governing,
        "profile": status.state.profile_name if status.state else None,
        "applied_at": status.state.applied_at if status.state else None,
        "plan_changed": status.plan_changed,
        "applied_to_plan": (
            {
                "guid": status.state.scheme_guid,
                "name": status.state.scheme_name,
            }
            if status.state and status.state.scheme_guid
            else None
        ),
        "settings": {
            key: {
                "label": SETTINGS[key].label,
                "available": bool(
                    status.readings.get(key) and status.readings[key].available
                ),
                "ac": status.readings[key].value_for(AC)
                if status.readings.get(key)
                else None,
                "dc": status.readings[key].value_for(DC)
                if status.readings.get(key)
                else None,
            }
            for key in WRITE_ORDER
        },
        "drift": [
            {"setting": key, "source": source, "current": live, "expected": expected}
            for key, source, live, expected in status.drift
        ],
        "restore_point": (
            {
                "captured_at": status.baseline.captured_at,
                "scheme_name": status.baseline.scheme_name,
                "scheme_guid": status.baseline.scheme_guid,
            }
            if status.baseline
            else None
        ),
    }


def cmd_profiles(args: argparse.Namespace) -> int:
    style = make_style(args)
    simulated_banner(args, style)
    controller = build_controller(args)
    profiles = controller.profiles()
    if args.json:
        print(
            json.dumps(
                {name: profile.to_dict() for name, profile in profiles.items()},
                indent=2,
            )
        )
        return 0
    print(style.heading("Profiles"))
    for name, profile in profiles.items():
        print(f"  {style.bold(name)} ({profile.origin})")
        print(f"    {profile.description}")
        targets = [
            f"{SETTINGS[key].label} {_format_value(key, value)}"
            for key, _source, value in profile.resolved_values()
        ]
        if targets:
            print(f"    Sets: {', '.join(dict.fromkeys(targets))}")
        if profile.partial:
            print(
                f"    {style.dim('Only the values you named are changed; everything else stays as it is.')}"
            )
    print("")
    print(f"Apply one with: {PROGRAM} apply --profile quiet")
    print(f"Save your own with: {PROGRAM} profile set my-quiet --max-state 70")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    style = make_style(args)
    simulated_banner(args, style)
    controller = build_controller(args)
    plan = controller.plan(args.profile, scope=args.scope)
    if args.json:
        print(json.dumps(_plan_dict(plan), indent=2))
        return 0
    print_plan(plan, style)
    print("")
    if plan.is_empty:
        print("Nothing would change.")
    else:
        print(
            f"This is a preview. To make these changes run: "
            f"{PROGRAM} apply --profile {args.profile}"
        )
        print("You will be asked to confirm after the plan is shown again.")
    return 0


def _plan_dict(plan: Plan) -> dict:
    return {
        "profile": plan.profile.name,
        "scheme": {"guid": plan.scheme_guid, "name": plan.scheme_name},
        "summary": plan.summary(),
        "changes": [
            {
                "setting": change.setting_key,
                "label": change.setting_label,
                "source": change.source,
                "current": change.current,
                "target": change.target,
                "description": change.describe(),
            }
            for change in plan.changes
        ],
        "unchanged": [
            {
                "setting": change.setting_key,
                "source": change.source,
                "value": change.target,
            }
            for change in plan.unchanged
        ],
        "checks": [
            {"setting": check.setting_key, "passed": check.passed, "detail": check.detail}
            for check in plan.checks
        ],
        "notes": list(plan.notes),
    }


def _confirmer(args: argparse.Namespace, style: Style, verb: str):
    def confirm(plan: Plan) -> bool:
        if getattr(args, "json", False) and not args.yes:
            # Keep stdout pure JSON: a program reading --json output cannot
            # answer a question, so asking would only corrupt its input.
            raise ActionRefused(
                "Nothing was changed: --json output cannot answer a "
                "confirmation question. Re-run with --yes once you have "
                f"reviewed '{PROGRAM} plan --profile {plan.profile.name} "
                "--json'."
            )
        if not getattr(args, "json", False):
            print_plan(plan, style)
            print("")
        if args.yes:
            if not getattr(args, "json", False):
                print("Confirmed with --yes, so the question was skipped.")
            return True
        if not sys.stdin.isatty():
            raise ActionRefused(
                f"Nothing was changed: there is no terminal to confirm in. "
                f"Re-run with --yes once you have reviewed the plan ("
                f"'{PROGRAM} plan --profile {plan.profile.name}' shows it "
                "without changing anything)."
            )
        answer = input(f"{verb} these {len(plan.changes)} change(s)? Type 'yes' to continue: ")
        if answer.strip().lower() in {"yes", "y"}:
            return True
        return False

    return confirm


def cmd_apply(args: argparse.Namespace) -> int:
    style = make_style(args)
    simulated_banner(args, style)
    controller = build_controller(args)
    if args.preview:
        result = controller.apply(args.profile, scope=args.scope, dry_run=True)
        if args.json:
            print(json.dumps(_plan_dict(result.plan), indent=2))
            return 0
        print_plan(result.plan, style)
        print("")
        print_result(result, style)
        return 0
    result = controller.apply(
        args.profile,
        scope=args.scope,
        confirm=_confirmer(args, style, "Apply"),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "applied": [_plan_change(c) for c in result.applied],
                    "verified": result.verified,
                    "baseline_created": result.baseline_created,
                    "restore_point": result.baseline_path,
                    "scheme": {"guid": result.plan.scheme_guid, "name": result.plan.scheme_name},
                },
                indent=2,
            )
        )
        return 0
    print_result(result, style)
    return 0


def _plan_change(change) -> dict:
    return {
        "setting": change.setting_key,
        "source": change.source,
        "from": change.current,
        "to": change.target,
    }


def cmd_restore(args: argparse.Namespace) -> int:
    style = make_style(args)
    simulated_banner(args, style)
    controller = build_controller(args)
    if args.preview:
        result = controller.restore(dry_run=True)
        if args.json:
            print(json.dumps(_plan_dict(result.plan), indent=2))
            return 0
        print_plan(result.plan, style)
        print("")
        print_result(result, style)
        return 0
    result = controller.restore(
        confirm=_confirmer(args, style, "Restore"),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "restored": [_plan_change(c) for c in result.applied],
                    "verified": result.verified,
                    "restore_point": result.baseline_path,
                },
                indent=2,
            )
        )
        return 0
    print_result(result, style, undo_hint=False)
    if result.changed:
        print("Your original processor settings are back in place.")
        print(
            f"The recorded values stay saved, so if you ever want them back "
            f"in a hurry: {PROGRAM} apply --profile quiet"
        )
    return 0


def cmd_profile_set(args: argparse.Namespace) -> int:
    style = make_style(args)
    controller = build_controller(args)
    scope = validate_scope(args.scope or "both")
    values: dict[str, dict[str, int]] = {}
    if args.max_state is not None:
        values["max_state"] = {AC: args.max_state, DC: args.max_state}
    if args.min_state is not None:
        values["min_state"] = {AC: args.min_state, DC: args.min_state}
    if args.boost is not None:
        values["boost_mode"] = {AC: _boost_value(args.boost), DC: _boost_value(args.boost)}
    if not values:
        raise QuietGovernorError(
            "Nothing to save: name at least one value, for example "
            f"'{PROGRAM} profile set {args.name} --max-state 70 --boost off'. "
            "Run 'quietgovernor profile set --help' to see the options."
        )
    if scope != "both":
        values = {
            key: {source: value for source, value in per_source.items() if source in
                  ({AC} if scope == "ac" else {DC})}
            for key, per_source in values.items()
        }
    profile = controller.save_profile(
        args.name, values, description=args.description or "", scope=scope
    )
    if args.json:
        print(json.dumps(profile.to_dict(), indent=2))
        return 0
    print(style.bold(f"Saved profile '{profile.name}'."))
    lines = {
        f"  {SETTINGS[key].label}: {_format_value(key, value)}"
        for key, _source, value in profile.resolved_values()
    }
    for line in sorted(lines):
        print(line)
    print("")
    print(f"Preview it with: {PROGRAM} plan --profile {profile.name}")
    return 0


def _boost_value(text: str) -> int:
    lowered = text.strip().lower()
    if lowered in BOOST_NAMES:
        return BOOST_NAMES[lowered]
    try:
        number = int(lowered)
    except ValueError:
        raise QuietGovernorError(
            f"{text!r} is not a boost setting. Use one of "
            f"{', '.join(sorted(BOOST_NAMES))}, or a number from 0 to 5. "
            "Run 'quietgovernor profile set --help' for what each means."
        ) from None
    return number


def cmd_profile_delete(args: argparse.Namespace) -> int:
    style = make_style(args)
    controller = build_controller(args)
    controller.delete_profile(args.name)
    if args.json:
        print(json.dumps({"deleted": args.name}))
        return 0
    print(style.bold(f"Deleted saved profile '{args.name}'."))
    print("A profile being deleted never changes your PC's settings.")
    return 0


def cmd_profile_show(args: argparse.Namespace) -> int:
    style = make_style(args)
    controller = build_controller(args)
    profile = controller.get_profile(args.name)
    if args.json:
        print(json.dumps(profile.to_dict(), indent=2))
        return 0
    print(style.heading(profile.name))
    print(profile.description)
    print("")
    print(f"Origin: {profile.origin}")
    print(f"Scope:  {profile.scope} power")
    if profile.partial:
        print("Only the values listed here are changed; the rest stay as they are.")
    for key, source, value in profile.resolved_values():
        print(
            f"  {SETTINGS[key].label} on {_source_label(source)} power: "
            f"{_format_value(key, value)}"
        )
    if not profile.resolved_values():
        print("  (no values: this profile only restores recorded settings)")
    return 0


# --- argument parsing ------------------------------------------------------


def cmd_autostart_install(args: argparse.Namespace) -> int:
    style = make_style(args)
    simulated_banner(args, style)
    controller = build_controller(args)
    manager = require_autostart(args, controller)
    profile = args.profile or DEFAULT_AUTOSTART_PROFILE

    if args.preview:
        plan, _changed = manager.install(
            profile,
            preview=True,
            allow_untrusted_paths=args.allow_untrusted_paths,
        )
        if args.json:
            print(json.dumps(_autostart_plan_dict(plan), indent=2))
            return 0
        _print_autostart_plan(plan, style)
        print("")
        print(
            style.dim(
                "Preview only: no scheduled task was created and no power "
                "settings were changed."
            )
        )
        return 0

    def confirm(plan: AutostartPlan) -> bool:
        if args.json and not args.yes:
            raise ActionRefused(
                "Nothing was changed: --json output cannot answer a confirmation "
                "question. Re-run with --yes once you have reviewed "
                f"'{PROGRAM} autostart install --profile {profile} --preview'."
            )
        if not args.json:
            _print_autostart_plan(plan, style)
            print("")
        if args.yes:
            if not args.json:
                print("Confirmed with --yes, so the question was skipped.")
            return True
        if not sys.stdin.isatty():
            raise ActionRefused(
                "Nothing was changed: there is no terminal to confirm in. Re-run "
                "with --yes once you have reviewed the plan."
            )
        answer = input(
            f"Create the sign-in task for the '{plan.profile}' profile? Type 'yes' to continue: "
        )
        return answer.strip().lower() in {"yes", "y"}

    plan, changed = manager.install(
        profile,
        confirm=confirm,
        allow_untrusted_paths=args.allow_untrusted_paths,
    )
    if args.json:
        payload = _autostart_plan_dict(plan)
        payload["created"] = changed
        print(json.dumps(payload, indent=2))
        return 0
    print("")
    if changed:
        print(style.bold(f"Created the sign-in task {plan.task_path}."))
        print(f"It will apply the '{plan.profile}' profile about 20 seconds after you sign in.")
        print("It runs with administrator rights, which is what changing power settings needs.")
        if args.allow_untrusted_paths:
            print(
                style.warn(
                    "This task runs code from a folder your account can write to, "
                    "because --allow-untrusted-paths was given."
                )
            )
        print(f"Remove it any time with: {PROGRAM} autostart remove")
    return 0


def cmd_autostart_remove(args: argparse.Namespace) -> int:
    style = make_style(args)
    simulated_banner(args, style)
    controller = build_controller(args)
    manager = require_autostart(args, controller)

    if args.preview:
        plan, _changed = manager.remove(preview=True)
        if args.json:
            print(json.dumps(_autostart_plan_dict(plan), indent=2))
            return 0
        _print_autostart_plan(plan, style)
        print("")
        print(style.dim("Preview only: the task is still registered."))
        return 0

    def confirm(plan: AutostartPlan) -> bool:
        if args.json and not args.yes:
            raise ActionRefused(
                "Nothing was changed: --json output cannot answer a confirmation "
                "question. Re-run with --yes to remove the task."
            )
        if not args.json:
            _print_autostart_plan(plan, style)
            if not args.yes:
                print("")
        if args.yes:
            return True
        if not sys.stdin.isatty():
            raise ActionRefused(
                "Nothing was changed: there is no terminal to confirm in. Re-run "
                "with --yes to remove the task."
            )
        return input("Remove the sign-in task? Type 'yes' to continue: ").strip().lower() in {
            "yes",
            "y",
        }

    plan, changed = manager.remove(confirm=confirm)
    if args.json:
        payload = _autostart_plan_dict(plan)
        payload["removed"] = changed
        print(json.dumps(payload, indent=2))
        return 0
    print("")
    if changed:
        print(style.bold(f"Removed the sign-in task {plan.task_path}."))
        print(
            "Your current processor settings are unchanged; only the automatic "
            f"re-apply at sign-in is gone. Undo the settings with: {PROGRAM} restore"
        )
    return 0


def cmd_autostart_run(args: argparse.Namespace) -> int:
    """The scheduled task's payload: apply one profile without a question.

    Consent was given when the task was installed, so there is nobody to ask.
    Output goes to the auto-start record instead of the screen, because the task
    runs under pythonw.exe where there is no console to write to.
    """
    style = make_style(args)
    controller = build_controller(args)
    manager = require_autostart(args, controller)
    profile = args.profile or DEFAULT_AUTOSTART_PROFILE
    record = manager.load_record()
    try:
        result = controller.apply(profile)
    except QuietGovernorError as error:
        manager.record_run(profile, False, str(error))
        if not args.json:
            print(f"Error: {error}", file=sys.stderr)
        else:
            print(json.dumps({"profile": profile, "ok": False, "error": str(error)}))
        return 1
    message = (
        f"Applied {len(result.applied)} setting(s) and verified each one."
        if result.changed
        else "Nothing needed changing; the PC already matched the profile."
    )
    if record is not None and record.profile != profile:
        message += (
            f" The installed task applies '{record.profile}', but this run used '{profile}'."
        )
    manager.record_run(profile, True, message)
    if args.json:
        print(json.dumps({"profile": profile, "ok": True, "changed": len(result.applied)}))
    elif not args.quiet:
        print(f"Auto-start run complete: {message}")
    return 0


def _print_autostart_plan(plan: AutostartPlan, style: Style) -> None:
    print(style.heading("Plan"))
    for line in plan.detail_lines():
        if line.startswith("  ! "):
            print("  " + style.warn("! " + line[4:]))
        elif line.startswith("Problems that stop this:"):
            print(style.warn(line))
        else:
            print(line)


def _autostart_plan_dict(plan: AutostartPlan) -> dict:
    return {
        "action": plan.action,
        "task_path": plan.task_path,
        "profile": plan.profile,
        "interpreter": plan.interpreter,
        "package_root": plan.package_root,
        "state_dir": plan.state_dir,
        "summary": plan.summary(),
        "blocked": plan.blocked,
        "checks": [
            {"name": check.key, "passed": check.passed, "severity": check.severity, "detail": check.detail}
            for check in plan.checks
        ],
        "notes": list(plan.notes),
    }


def _autostart_status_dict(status: AutostartStatus) -> dict:
    return {
        "available": True,
        "task_path": status.task_path,
        "installed": status.installed,
        "consistent": status.consistent,
        "profile": status.recorded.profile if status.recorded else None,
        "installed_at": status.recorded.installed_at if status.recorded else None,
        "run_level": (status.live or {}).get("run_level"),
        "command": (status.live or {}).get("command"),
        "arguments": (status.live or {}).get("arguments"),
        "problems": list(status.problems),
        "last_run": status.last_run.to_dict() if status.last_run else None,
    }


def _profile_parser() -> argparse.ArgumentParser:
    """The 'profile' sub-parser, rebuilt on demand for help output."""
    parser = build_parser()
    for action in parser._actions:  # noqa: SLF001 - argparse has no public accessor
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return action.choices["profile"]
    raise RuntimeError("profile sub-parser is missing")  # pragma: no cover


def _common_options() -> argparse.ArgumentParser:
    """Options accepted before or after the subcommand.

    Defaults are SUPPRESS so a subparser parse cannot overwrite a value the
    main parser already set (``--json`` before the command would otherwise be
    reset by the subparser's own default).
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--simulate",
        action="store_true",
        default=argparse.SUPPRESS,
        help="use an in-memory PC instead of Windows (safe on any OS)",
    )
    common.add_argument(
        "--state-dir",
        metavar="DIR",
        default=argparse.SUPPRESS,
        help=f"where state files live (default: {default_state_dir()})",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print machine-readable JSON",
    )
    common.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="never use terminal colours",
    )
    return common


def _autostart_parser() -> argparse.ArgumentParser:
    """The 'autostart' sub-parser, rebuilt on demand for help output."""
    parser = build_parser()
    for action in parser._actions:  # noqa: SLF001 - argparse has no public accessor
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return action.choices["autostart"]
    raise RuntimeError("autostart sub-parser is missing")  # pragma: no cover


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Keep a Windows PC quiet by capping processor performance states. "
            "Every change is previewed, recorded, verified, and reversible."
        ),
        epilog=(
            "Examples:\n"
            f"  {PROGRAM} status\n"
            f"  {PROGRAM} plan --profile quiet\n"
            f"  {PROGRAM} apply --profile quiet\n"
            f"  {PROGRAM} restore --dry-run\n"
            f"  {PROGRAM} --simulate apply --profile quiet --yes\n"
            f"  {PROGRAM} autostart install --profile quiet   (optional, at sign-in)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, **kwargs):
        """Register a subcommand that also accepts the common options."""
        return subparsers.add_parser(name, parents=[common], **kwargs)


    status = add("status", help="show current settings and state")
    status.set_defaults(func=cmd_status)

    profiles = add("profiles", help="list available profiles")
    profiles.set_defaults(func=cmd_profiles)

    plan = add("plan", help="preview what a profile would change")
    plan.add_argument("--profile", "-p", default="quiet", help="profile name (default: quiet)")
    plan.add_argument("--scope", choices=["both", "ac", "dc"], help="power sources to change")
    plan.set_defaults(func=cmd_plan)

    apply = add(
        "apply", help="preview, confirm, then change settings"
    )
    apply.add_argument("--profile", "-p", default="quiet", help="profile name (default: quiet)")
    apply.add_argument("--scope", choices=["both", "ac", "dc"], help="power sources to change")
    apply.add_argument(
        "--preview",
        "--dry-run",
        dest="preview",
        action="store_true",
        help="show the plan without changing anything",
    )
    apply.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    apply.set_defaults(func=cmd_apply)

    restore = add(
        "restore", help="put your original settings back"
    )
    restore.add_argument(
        "--preview",
        "--dry-run",
        dest="preview",
        action="store_true",
        help="show what restore would change",
    )
    restore.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    restore.set_defaults(func=cmd_restore)

    autostart = add("autostart", help="optional: re-apply a profile at sign-in")
    autostart_sub = autostart.add_subparsers(dest="autostart_command", metavar="ACTION")

    def add_autostart(name: str, **kwargs):
        """Nested subcommand that also accepts the common options."""
        return autostart_sub.add_parser(name, parents=[common], **kwargs)

    install_parser = add_autostart(
        "install", help="register a sign-in task that re-applies a profile"
    )
    install_parser.add_argument(
        "--profile", "-p", default=None, help=f"profile to apply (default: {DEFAULT_AUTOSTART_PROFILE})"
    )
    install_parser.add_argument(
        "--preview", "--dry-run", dest="preview", action="store_true",
        help="show what would be created without creating it",
    )
    install_parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    install_parser.add_argument(
        "--allow-untrusted-paths",
        action="store_true",
        help="create the task even if the interpreter or package is in a folder "
        "your account can write to (weakens the protection against privilege escalation)",
    )
    install_parser.set_defaults(func=cmd_autostart_install)

    remove_parser = add_autostart("remove", help="delete the sign-in task")
    remove_parser.add_argument(
        "--preview", "--dry-run", dest="preview", action="store_true",
        help="show what would be removed",
    )
    remove_parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    remove_parser.set_defaults(func=cmd_autostart_remove)

    run_parser = add_autostart(
        "run", help="apply a profile silently (this is what the task runs)"
    )
    run_parser.add_argument(
        "--profile", "-p", default=None, help=f"profile to apply (default: {DEFAULT_AUTOSTART_PROFILE})"
    )
    run_parser.add_argument(
        "--quiet", "-q", action="store_true", help="no output (the default under pythonw.exe)"
    )
    run_parser.set_defaults(func=cmd_autostart_run)

    profile = add("profile", help="manage your own profiles")
    profile_sub = profile.add_subparsers(dest="profile_command", metavar="ACTION")

    set_parser = profile_sub.add_parser("set", help="save a profile with your own values")
    set_parser.add_argument("name", help="profile name, for example 'night'")
    set_parser.add_argument(
        "--max-state",
        type=int,
        metavar="PERCENT",
        help="highest processor state to allow, 20-100 (lower is quieter)",
    )
    set_parser.add_argument(
        "--min-state",
        type=int,
        metavar="PERCENT",
        help="lowest processor state to allow, 0-50 (higher keeps clocks up)",
    )
    set_parser.add_argument(
        "--boost",
        metavar="MODE",
        help="boost mode: off, on, aggressive, efficient, or 0-5",
    )
    set_parser.add_argument("--scope", choices=["both", "ac", "dc"], default="both",
                            help="power sources this profile governs (default: both)")
    set_parser.add_argument("--description", help="a sentence describing the profile")
    set_parser.set_defaults(func=cmd_profile_set)

    delete_parser = profile_sub.add_parser("delete", help="delete a saved profile")
    delete_parser.add_argument("name", help="profile name")
    delete_parser.set_defaults(func=cmd_profile_delete)

    show_parser = profile_sub.add_parser("show", help="show one profile in detail")
    show_parser.add_argument("name", help="profile name")
    show_parser.set_defaults(func=cmd_profile_show)

    return parser


#: Common options use SUPPRESS defaults (see _common_options), so restore the
#: documented defaults after parsing instead of relying on hasattr everywhere.
_COMMON_DEFAULTS = {"simulate": False, "json": False, "no_color": False, "state_dir": None}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, default in _COMMON_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    if args.command == "profile" and not getattr(args, "profile_command", None):
        _profile_parser().print_help()
        return 2
    if args.command == "autostart" and not getattr(args, "autostart_command", None):
        _autostart_parser().print_help()
        return 2
    try:
        return int(args.func(args))
    except ActionRefused as refusal:
        print(f"{refusal}", file=sys.stderr)
        return 0
    except QuietGovernorError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Nothing further was changed.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
