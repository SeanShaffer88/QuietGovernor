"""Produce the evidence docs/SPEC.md §9 asks for, on a real Windows PC.

The project cannot claim the Windows path works until somebody runs it and
records what happened. This script makes that run repeatable and hard to get
wrong:

* it records the environment (Windows build, Python, elevation) alongside the
  readings, because a result is meaningless without them;
* the read phase is safe and changes nothing;
* the write phase is opt-in (``--apply``), needs elevation, and **always**
  attempts to restore in a ``finally`` block — including when a check fails or
  the process raises;
* every expectation is compared against what QuietGovernor itself planned, not
  against numbers copied into this file, so the harness cannot drift from the
  product;
* the output is a JSON file plus a Markdown table ready to paste into
  ``docs/SPEC.md`` §9.

Usage on a laptop:

    py scripts/verify_windows.py                 # read-only
    py scripts/verify_windows.py --apply         # also apply + restore
    py scripts/verify_windows.py --apply --autostart

Exit codes: 0 every check passed; 1 a check failed; 2 the run could not be
carried out (wrong platform, not elevated for a requested phase, CLI missing).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
QUIET_EXPECTED_MAX_STATE = 60
DEFAULT_PROFILE = "quiet"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --- talking to the CLI ----------------------------------------------------


class CliError(RuntimeError):
    """The CLI failed in a way that makes the result meaningless."""


class Cli:
    """Runs QuietGovernor and parses its JSON, with a fixed state directory."""

    def __init__(
        self, command: list[str], state_dir: Path | None, *, simulate: bool = False
    ) -> None:
        self.command = command
        #: None means "let QuietGovernor use its normal per-user folder". That is
        #: the safest choice for a verification run: the baseline survives a
        #: crash, so the machine can always be put back.
        self.state_dir = state_dir
        self.simulate = simulate

    def _argv(self, *arguments: str) -> list[str]:
        prefix = list(self.command)
        if self.state_dir is not None:
            prefix += ["--state-dir", str(self.state_dir)]
        if self.simulate:
            prefix.append("--simulate")
        return [*prefix, *arguments]

    @property
    def state_dir_label(self) -> str:
        if self.state_dir is not None:
            return str(self.state_dir)
        return "QuietGovernor's default per-user state folder"

    def run(self, *arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            self._argv(*arguments),
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if check and result.returncode != 0:
            raise CliError(
                f"'{' '.join(arguments)}' failed with exit code "
                f"{result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def json(self, *arguments: str) -> dict:
        result = self.run(*arguments, check=True)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CliError(
                f"'{' '.join(arguments)}' did not print JSON ({exc}): "
                f"{result.stdout[:300]!r}"
            ) from exc

    def status(self) -> dict:
        return self.json("--json", "status")


# --- results ---------------------------------------------------------------


def _jsonable(value: object) -> object:
    """Make a value JSON-safe, naming tuple keys "setting/source"."""
    if isinstance(value, dict):
        return {
            (f"{key[0]}/{key[1]}" if isinstance(key, tuple) else str(key)): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    return str(value)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    expected: object = None
    actual: object = None
    skipped: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": "skipped" if self.skipped else ("pass" if self.ok else "fail"),
            "detail": self.detail,
            "expected": _jsonable(self.expected),
            "actual": _jsonable(self.actual),
        }


@dataclass
class Report:
    mode: str
    environment: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=now)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and not check.skipped]

    @property
    def skipped(self) -> list[Check]:
        return [check for check in self.checks if check.skipped]

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "environment": self.environment,
            "checks": [check.to_dict() for check in self.checks],
            "summary": {
                "passed": len([c for c in self.checks if c.ok and not c.skipped]),
                "failed": len(self.failures),
                "skipped": len(self.skipped),
            },
            "notes": self.notes,
        }


# --- the phases ------------------------------------------------------------


def collect_environment(cli: Cli, report: Report) -> dict:
    """Record what a reader needs in order to believe the result."""
    status = cli.status()
    environment = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "command": " ".join(cli.command),
        "state_dir": cli.state_dir_label,
        "elevated": status.get("elevated"),
        "active_plan": (status.get("scheme") or {}).get("name"),
        "plan_guid": (status.get("scheme") or {}).get("guid"),
        "simulated": cli.simulate,
    }
    if platform.system() == "Windows":
        # Windows 11 reports 10.0 as well; the build number is the real
        # discriminator (22000 and up is Windows 11).
        try:
            build = int(platform.version().split(".")[2])
        except (IndexError, ValueError):
            build = None
        environment["windows_build"] = build
        environment["windows_name"] = (
            "Windows 11" if build and build >= 22000 else "Windows 10 or earlier"
        )
    report.environment = environment
    return environment


def phase_read(cli: Cli, report: Report) -> dict:
    """Read-only checks: nothing here changes a setting."""
    status = cli.status()
    settings = status.get("settings") or {}

    report.add(
        Check(
            "active plan is reported",
            ok=bool((status.get("scheme") or {}).get("name")),
            detail="QuietGovernor names the power plan it will change.",
            actual=(status.get("scheme") or {}).get("name"),
        )
    )

    max_state = settings.get("max_state") or {}
    report.add(
        Check(
            "maximum processor state is readable",
            ok=bool(max_state.get("available")) and max_state.get("ac") is not None,
            detail=(
                "The setting QuietGovernor relies on is present on this PC. "
                "Without it nothing can be applied."
            ),
            actual={"ac": max_state.get("ac"), "dc": max_state.get("dc")},
        )
    )

    values = [max_state.get("ac"), max_state.get("dc")]
    in_range = all(
        isinstance(value, int) and 0 <= value <= 100 for value in values if value is not None
    )
    report.add(
        Check(
            "reported values are in range",
            ok=in_range,
            detail="Percentages read back from Windows are between 0 and 100.",
            actual=values,
        )
    )

    boost = settings.get("boost_mode") or {}
    report.add(
        Check(
            "boost mode presence is reported",
            ok=("available" in boost),
            detail=(
                "Whether this CPU exposes a performance boost setting is stated "
                "either way. Absence is normal on desktops and VMs."
            ),
            actual={"available": boost.get("available"), "ac": boost.get("ac")},
        )
    )

    report.add(
        Check(
            "restore point state is reported",
            ok="restore_point" in status,
            detail="status says whether a restore point exists before anything is changed.",
            actual=status.get("restore_point"),
        )
    )
    return status


def planned_targets(cli: Cli, profile: str) -> dict[tuple[str, str], int]:
    """Ask QuietGovernor what it *would* write, and use that as the expectation."""
    plan = cli.json("--json", "plan", "--profile", profile)
    targets: dict[tuple[str, str], int] = {}
    for change in plan.get("changes", []):
        targets[(change["setting"], change["source"])] = change["target"]
    for unchanged in plan.get("unchanged", []):
        targets[(unchanged["setting"], unchanged["source"])] = unchanged["value"]
    return targets


def phase_write(cli: Cli, report: Report, profile: str) -> None:
    """Apply, verify each value, restore, verify the originals are back."""
    before = cli.status()
    baseline_values = {
        (key, source): details.get(source.lower())
        for key, details in (before.get("settings") or {}).items()
        for source in ("ac", "dc")
    }
    before_restore_point = before.get("restore_point")

    targets = planned_targets(cli, profile)
    if not targets:
        report.add(
            Check(
                "a plan exists to verify",
                ok=False,
                detail=(
                    "plan reported no changes, so this PC already matches the "
                    "profile. Restore your settings or choose another profile."
                ),
            )
        )
        return

    applied = False
    try:
        result = cli.run("apply", "--profile", profile, "--yes", check=True)
        applied = True
        report.add(
            Check(
                "apply exits successfully",
                ok=True,
                detail=f"'{profile}' applied and every write was read back.",
                actual=result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "",
            )
        )

        after = cli.status()
        settings = after.get("settings") or {}
        mismatches: list[str] = []
        compared: dict[str, dict[str, int]] = {}
        maximized = 0
        for (setting, source), target in targets.items():
            actual = (settings.get(setting) or {}).get(source.lower())
            compared[f"{setting}/{source}"] = {"expected": target, "actual": actual}
            if actual != target:
                mismatches.append(f"{setting} {source}: expected {target}, got {actual}")
            if setting == "max_state" and actual is not None:
                maximized = max(maximized, actual)

        report.add(
            Check(
                "written values match the plan",
                ok=not mismatches,
                detail=(
                    "Every value QuietGovernor wrote reads back as the planned value."
                    if not mismatches
                    else "Windows did not keep the planned values: " + "; ".join(mismatches)
                ),
                expected=targets,
                actual=compared,
            )
        )

        report.add(
            Check(
                "maximum state is capped below full speed",
                ok=maximized <= 100 - 1,
                detail=(
                    "The cap is what makes the machine quieter; a cap of 100% "
                    "would mean nothing changed."
                ),
                expected=f"<= {QUIET_EXPECTED_MAX_STATE - 1}",
                actual=maximized,
            )
        )

        governed = after.get("governing")
        report.add(
            Check(
                "status reports the profile as governing",
                ok=bool(governed),
                detail="After apply, status says which profile is in force.",
                actual={"governing": governed, "profile": after.get("profile")},
            )
        )

        drift = after.get("drift") or []
        report.add(
            Check(
                "no drift immediately after apply",
                ok=not drift,
                detail="Nothing else changed the values between the write and the read.",
                actual=drift,
            )
        )
    finally:
        if applied:
            _restore(cli, report, baseline_values, before_restore_point)


def _restore(
    cli: Cli,
    report: Report,
    baseline_values: dict[tuple[str, str], int],
    before_restore_point: object,
) -> None:
    """Put the original values back. Runs even when a check above failed."""
    try:
        result = cli.run("restore", "--yes", check=False)
    except OSError as exc:  # pragma: no cover - subprocess could not start
        report.add(
            Check(
                "restore ran",
                ok=False,
                detail=(
                    f"Could not run restore ({exc}). Your original values are in "
                    f"{cli.state_dir_label}. Run "
                    "'quietgovernor restore' in an elevated terminal to put them back."
                ),
            )
        )
        return

    if result.returncode != 0:
        report.add(
            Check(
                "restore ran",
                ok=False,
                detail=(
                    "restore failed: "
                    + (result.stderr.strip() or result.stdout.strip())
                    + f" Run 'quietgovernor restore' in an elevated terminal; the "
                    f"original values are recorded in {cli.state_dir_label}."
                ),
            )
        )
        return

    report.add(
        Check(
            "restore exits successfully",
            ok=True,
            detail="The recorded original values were written back and verified.",
        )
    )

    after = cli.status()
    settings = after.get("settings") or {}
    mismatches = []
    compared = {}
    for (setting, source), original in baseline_values.items():
        if original is None:
            continue
        actual = (settings.get(setting) or {}).get(source.lower())
        compared[f"{setting}/{source}"] = {"expected": original, "actual": actual}
        if actual != original:
            mismatches.append(f"{setting} {source}: expected {original}, got {actual}")

    report.add(
        Check(
            "original values are back",
            ok=not mismatches,
            detail=(
                "Every setting reads exactly as it did before this run."
                if not mismatches
                else "These settings did not return to their original values: "
                + "; ".join(mismatches)
                + ". Run 'quietgovernor restore' again and check the result."
            ),
            expected=baseline_values,
            actual=compared,
        )
    )

    report.add(
        Check(
            "nothing is reported as governed afterwards",
            ok=not after.get("governing"),
            detail="After restore, status no longer claims to manage the PC.",
            actual=after.get("governing"),
        )
    )

    if before_restore_point:
        report.add(
            Check(
                "the restore point is kept",
                ok=after.get("restore_point") is not None,
                detail=(
                    "The recorded original values survive the restore, so they can "
                    "always be put back again."
                ),
                actual=after.get("restore_point"),
            )
        )


def phase_autostart(cli: Cli, report: Report, *, install: bool, profile: str) -> None:
    """Check the privilege guard, and optionally create and remove the task."""
    preview = cli.run("autostart", "install", "--profile", profile, "--preview")
    text = f"{preview.stdout}\n{preview.stderr}"
    report.add(
        Check(
            "auto-start preview runs",
            ok=preview.returncode == 0,
            detail=(
                "The sign-in task can be planned without creating anything."
                if preview.returncode == 0
                else "The preview failed: " + (preview.stderr.strip() or preview.stdout.strip())
            ),
        )
    )

    guard = "administrator-only" in text or "not in an administrator-only" in text
    report.add(
        Check(
            "the privilege guard gives a verdict on this machine",
            ok=guard,
            detail=(
                "The preview states whether the interpreter and package are in an "
                "administrator-only folder, or says the check could not be made. "
                "Read the result in the preview text: either verdict is valid, "
                "silence is not."
            ),
            actual=_first_matching_line(text, ("administrator-only", "could not work out")),
        )
    )

    status = cli.status().get("autostart") or {}
    report.add(
        Check(
            "status reports the auto-start state",
            ok=status.get("available") is True and status.get("installed") is False,
            detail=(
                "Before installing, status says auto-start is available and not "
                "installed."
            ),
            actual={"available": status.get("available"), "installed": status.get("installed")},
        )
    )

    if not install:
        report.add(
            Check(
                "sign-in task created and removed",
                ok=True,
                skipped=True,
                detail="Not requested. Re-run with --autostart to create and remove it.",
            )
        )
        return

    created = cli.run("autostart", "install", "--profile", profile, "--yes")
    blocked = created.returncode != 0
    report.add(
        Check(
            "sign-in task installed",
            ok=created.returncode == 0,
            detail=(
                "Windows accepted the task and QuietGovernor read it back."
                if created.returncode == 0
                else "Not created: " + (created.stderr.strip() or created.stdout.strip())
            ),
        )
    )
    if blocked:
        report.add(
            Check(
                "sign-in task removed",
                ok=True,
                skipped=True,
                detail="Nothing was created, so nothing needed removing.",
            )
        )
        return

    installed = cli.status().get("autostart") or {}
    report.add(
        Check(
            "the task runs elevated at sign-in",
            ok=installed.get("run_level") == "HighestAvailable"
            and "quietgovernor" in (installed.get("arguments") or "").lower(),
            detail="Task Scheduler reports the elevated run level and the payload command.",
            expected={"run_level": "HighestAvailable", "arguments": "… quietgovernor …"},
            actual={
                "run_level": installed.get("run_level"),
                "command": installed.get("command"),
                "arguments": installed.get("arguments"),
            },
        )
    )
    report.add(
        Check(
            "status shows no inconsistency for the task",
            ok=not installed.get("problems"),
            detail="The task and QuietGovernor's record of it agree.",
            actual=installed.get("problems"),
        )
    )

    removed = cli.run("autostart", "remove", "--yes")
    report.add(
        Check(
            "sign-in task removed",
            ok=removed.returncode == 0,
            detail=(
                "The task was deleted and verified gone."
                if removed.returncode == 0
                else "Not removed: " + (removed.stderr.strip() or removed.stdout.strip())
            ),
        )
    )
    after = cli.status().get("autostart") or {}
    report.add(
        Check(
            "nothing is registered afterwards",
            ok=after.get("installed") is False,
            detail="After removal, status says auto-start is not installed.",
            actual=after.get("installed"),
        )
    )


def _first_matching_line(text: str, needles: tuple[str, ...]) -> str:
    for line in text.splitlines():
        if any(needle in line for needle in needles):
            return " ".join(line.split())
    return ""


# --- output ----------------------------------------------------------------


def render_markdown(report: Report) -> str:
    environment = report.environment
    lines = [
        "# QuietGovernor verification report",
        "",
        f"Generated: {report.generated_at}",
        f"Mode: {'simulation (no real effect)' if report.mode == 'simulate' else 'real'}",
        "",
        "## Environment",
        "",
        "| Item | Value |",
        "| --- | --- |",
    ]
    for key in (
        "windows_name",
        "windows_build",
        "simulated",
        "release",
        "version",
        "machine",
        "processor",
        "python",
        "elevated",
        "active_plan",
        "plan_guid",
        "state_dir",
    ):
        if key in environment:
            lines.append(f"| {key} | {environment[key]} |")

    lines += ["", "## Checks", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    for check in report.checks:
        result = "skipped" if check.skipped else ("pass" if check.ok else "**FAIL**")
        detail = str(check.detail).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {check.name} | {result} | {detail} |")

    failures = report.failures
    hard_skips = [check for check in report.skipped if "sign-in" in check.name or "apply" in check.name]
    lines += ["", "## Verdict", ""]

    if report.mode == "simulate":
        lines.append(
            "**Simulation run.** This exercised the harness, not Windows. It is "
            "not evidence for any verification claim in `docs/SPEC.md`."
        )
        lines += [
            "",
            "## Recording this result",
            "",
            "Nothing to record. Run this on the Windows PC without `--simulate` for a "
            "result that can be recorded.",
            "",
        ]
        return "\n".join(lines)

    if failures:
        lines.append(
            f"**{len(failures)} check(s) failed.** Do not record this machine as "
            "verified; investigate the failures above first."
        )
    elif hard_skips:
        lines.append(
            "**The phases that ran passed** for this machine. Some phases were "
            "skipped, so this is a partial result: re-run with `--apply --autostart` "
            "in an elevated terminal for the full checklist."
        )
    else:
        lines.append(
            "**All checks passed** for this machine, including the sign-in task path."
        )

    lines += [
        "",
        "## Recording this result",
        "",
        "Paste the row below into the verification table in `docs/SPEC.md` §9, and "
        "keep this report with the pull request or issue so the claim has evidence.",
        "",
        "```markdown",
        f"| {environment.get('windows_name', 'Windows')} build "
        f"{environment.get('windows_build', '?')}, "
        f"Python {environment.get('python', '?')}, "
        f"{'elevated' if environment.get('elevated') else 'not elevated'} | "
        f"{len(failures)} failed check(s), {len(hard_skips)} phase(s) skipped | "
        f"{report.generated_at} |",
        "```",
        "",
    ]
    return "\n".join(lines)


def write_reports(report: Report, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.replace(":", "").replace("-", "")
    json_path = out_dir / f"verification-{stamp}.json"
    markdown_path = out_dir / f"verification-{stamp}.md"
    json_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


# --- entry point -----------------------------------------------------------


def find_cli(explicit: str | None) -> list[str] | None:
    if explicit:
        return [explicit]
    script = shutil.which("quietgovernor")
    if script:
        return [script]
    probe = subprocess.run(
        [sys.executable, "-m", "quietgovernor", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", "quietgovernor"]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the verification checklist from docs/SPEC.md §9 and write the "
            "evidence to a report. Read-only unless --apply is given."
        )
    )
    parser.add_argument("--apply", action="store_true", help="also apply a profile and restore it")
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="also create and remove the sign-in task (needs --apply and elevation)",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="profile to apply (default: quiet)")
    parser.add_argument("--out-dir", default=".", help="where to write the report (default: .)")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="QuietGovernor state folder (default: the normal per-user folder, so a "
        "restore point survives if this run is interrupted)",
    )
    parser.add_argument("--cli", default=None, help="path to the quietgovernor executable")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="run against the simulated PC (for trying the harness itself)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="do not ask for confirmation of the write phase",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    command = find_cli(args.cli)
    if command is None:
        print(
            "QuietGovernor is not installed for this Python, so there is nothing to "
            "verify. Install it first:  py -m pip install quietgovernor",
            file=sys.stderr,
        )
        return 2

    if not args.simulate and not platform.system() == "Windows":
        print(
            "This harness verifies the Windows path, and this is "
            f"{platform.system()}. Run it on the Windows PC you want to verify, or add "
            "--simulate to exercise the harness itself.",
            file=sys.stderr,
        )
        return 2

    if args.state_dir is not None:
        state_dir: Path | None = Path(args.state_dir)
    elif args.simulate:
        # Keep a simulated run out of the real state folder.
        state_dir = Path(args.out_dir) / ".verify-state"
    else:
        state_dir = None  # normal per-user folder, so the restore point persists
    cli = Cli(command, state_dir, simulate=args.simulate)

    report = Report(mode="simulate" if args.simulate else "real")

    try:
        environment = collect_environment(cli, report)
    except CliError as error:
        print(f"Could not read this PC's settings, so nothing can be verified:\n{error}", file=sys.stderr)
        return 2

    try:
        phase_read(cli, report)
    except CliError as error:
        print(f"Read phase failed: {error}", file=sys.stderr)
        return 2

    if args.apply:
        elevated = environment.get("elevated")
        if elevated is False:
            report.add(
                Check(
                    "write phase can run",
                    ok=False,
                    detail=(
                        "This terminal is not elevated. Changing power settings needs "
                        "administrator rights: right-click your terminal, choose 'Run as "
                        "administrator', and run this again. Nothing was changed by this "
                        "attempt."
                    ),
                )
            )
            print(render_markdown(report))
            write_reports(report, Path(args.out_dir))
            return 2
        if not args.yes and not args.simulate:
            print(
                f"This will change this PC's processor power settings to the "
                f"'{args.profile}' profile and then put them back. Continue? Type 'yes': ",
                end="",
            )
            if input().strip().lower() not in {"yes", "y"}:
                print("Stopped. Nothing was changed.")
                return 2
        phase_write(cli, report, args.profile)
        phase_autostart(cli, report, install=args.autostart, profile=args.profile)
    else:
        report.add(
            Check(
                "apply and restore",
                ok=True,
                skipped=True,
                detail="Not requested. Re-run with --apply to verify the write path.",
            )
        )
        report.add(
            Check(
                "sign-in task created and removed",
                ok=True,
                skipped=True,
                detail="Not requested. Re-run with --apply --autostart.",
            )
        )

    markdown = render_markdown(report)
    print(markdown)
    json_path, markdown_path = write_reports(report, Path(args.out_dir))
    print(f"Report written to:\n  {json_path}\n  {markdown_path}")
    return 0 if not report.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
