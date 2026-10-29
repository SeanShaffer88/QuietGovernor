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
        print(json.dumps(_status_dict(status), indent=2))
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
        if status.drift:
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
        else:
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
    if not args.simulate:
        print("")
        print(style.dim(f"State folder: {build_store(args).directory}"))
        print(style.dim(f"Elevated: {'yes' if _elevated() else 'no'}"
                        " (changing settings needs administrator rights)"))
    return 0


def _elevated() -> bool:
    try:
        from .windows import is_elevated
    except Exception:  # pragma: no cover - import trouble
        return False
    return is_elevated()


def _status_dict(status: Status) -> dict:
    return {
        "scheme": {"guid": status.scheme_guid, "name": status.scheme_name},
        "governing": status.governing,
        "profile": status.state.profile_name if status.state else None,
        "applied_at": status.state.applied_at if status.state else None,
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
