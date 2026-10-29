# Changelog

All notable changes to QuietGovernor are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] — 2026-10-29

### Fixed

- `docs/VERIFY.md` — the page a person follows to close the hardware gap — still
  said `pip install quietgovernor`, which cannot work: the name is not on PyPI.
  It now points at the release wheel and notes that the verification script ships
  with the source, not the wheel. The README requirements table had the same
  command.
- `quietgovernor apply` (and `autostart install`/`remove`) with no terminal and no
  `--yes` refused to act but exited **0**, so a script or scheduled task that
  forgot `--yes` looked successful while changing nothing. Refusing because there
  is no way to ask now exits **1**. A person answering "no" at the prompt still
  exits 0 — that is a decision, not a failure.
- `--version` was documented as working before or after the command name, but
  `quietgovernor status --version` failed with a usage error. It is now part of
  the shared options, so it works in every position, including
  `quietgovernor autostart install --version`.
- After a power-plan switch, `restore` wrote the recorded values into whatever
  plan was active, reported "Your original processor settings are back in place",
  and cleared its state — while the plan it had actually changed kept the cap, now
  forgotten. Windows keeps these settings per plan, so that was a silent failure
  to undo. `restore` now refuses in that situation (exit 1), names both plans, and
  says how to finish: switch to the plan that was changed and run it again, or
  pass `--into-active-plan`. The restore point is kept either way.
- The `apply` preview still described the old restore rule ("writes it into the
  active plan"). It now says restore will refuse and names the two ways forward; a
  test pins the wording to the behaviour.

### Changed

- `docs/SPEC.md` §4 spells out which exit status each kind of refusal produces,
  and the preview row no longer implies `plan --dry-run` is a valid command
  (`plan` is itself the preview; `--preview`/`--dry-run` belong to the commands
  that write).

## [0.3.0] — 2026-10-29

### Fixed

- Saving a profile under a built-in name (`profile set quiet ...`) used to shadow
  the built-in everywhere, and `profile delete quiet` then refused because the
  name was built-in — leaving a profile that could not be removed. Saving under a
  built-in name is now refused (case-insensitively) with a suggested alternative,
  and a profile written under such a name by 0.2.0 can still be deleted, which
  restores the built-in.
- After switching Windows power plans, `status` reported the difference as drift
  ("something else changed them"), which pointed at the wrong cause. It now says
  the active plan is not the plan the profile was applied to, names both plans,
  and offers the re-apply. `--json` gains `plan_changed` and `applied_to_plan`.
- Re-applying after a plan switch, when the new plan already matched the profile,
  wrote nothing and left the record pointing at the old plan — so the notice could
  never clear. An empty apply now re-points the record at the active plan.
- The README told people to `pip install quietgovernor`. The package is not on
  PyPI, so that failed; installation now points at the wheel attached to a release
  (or a checkout), and the wheel filename is the one this release actually
  produces.

### Changed

- Saved profile names are validated: 1–40 characters of letters, digits, `.`, `_`,
  or `-`, starting with a letter or digit. Messages suggest a valid name.

### Added

- `scripts/verify_windows.py`: runs the `docs/SPEC.md` §9 hardware checklist on a
  real PC and writes a JSON plus Markdown report (environment, Windows build,
  elevation, expected and actual value for every setting, and a pasteable row for
  the verification table). Read-only unless `--apply` is given; the write phase
  always attempts a restore in a `finally` block, compares the written values
  against QuietGovernor's own plan rather than copied constants, and refuses to
  present a simulated or non-Windows run as evidence.
- `--autostart-anyway`: on a machine where the privilege guard declines (a
  per-user Python, or a CI runner's tool cache), create the task anyway to
  exercise Task Scheduler and the task XML, then remove it. Recorded as an
  override in the report; for throw-away machines only.
- `docs/VERIFY.md`: the operator procedure, including what no script can check
  (whether the fan is actually quieter), how to read each failure, and the second
  machine coverage worth having.
- The Windows CI job now runs the harness as its evidence step and fails on any
  failed check, so `schtasks.exe` and the Task Scheduler XML moved from
  **Unverified** to **Partially verified** (virtual machine only; no human has
  inspected the resulting values).
- `--json status` now reports `elevated` (`null` in simulation), so a report can
  state whether writes were even possible.
- `.github/workflows/release.yml`: on a `v*` tag, checks the tag matches the
  package version, runs the suite, builds a wheel and sdist, installs the wheel in
  a clean environment and drives the harness against it, then publishes a GitHub
  release with both files. Until now there was no distributable at all.
- 27 new tests for the harness, including the guarantee that restore
  runs after a failed check and that a failed restore is reported with the way
  back.

## [0.2.0] — 2026-10-29

### Added

- **Optional sign-in auto-start**, off by default and never enabled implicitly:
  - `autostart install [--profile NAME]` registers one Task Scheduler entry
    (`\QuietGovernor\ApplyAtLogon`) that re-applies a profile about 20 seconds
    after sign-in, as the user, with highest available privileges, ignoring
    battery/network/idle conditions, under `pythonw.exe` so no console flashes.
  - `autostart remove` deletes it and verifies it is gone.
  - `autostart run` is the task's payload: it applies one profile without asking
    (consent was given at install time) and records success or the error text so
    `status` can show it.
  - `autostart install --preview` / `remove --preview`, plus `--json` output.
- **Privilege-escalation guard.** Because a scheduled task runs whatever program
  it points at with administrator rights, install is refused unless the Python
  interpreter and the QuietGovernor package are both inside an administrator-only
  folder (`Program Files`, `%SystemRoot%`). A default per-user Python install does
  not qualify, and the error says how to fix it. If those folders cannot be
  identified, the check fails closed. `--allow-untrusted-paths` overrides it and
  the override is reported in the plan, the result, and `status`.
- Read-back verification of the created task: a task that Task Scheduler reports
  as missing, or with a different command, fails the install with an explanation.
- `status` gains a "Sign-in auto-start" section (task, profile, privileges, last
  run and its outcome, and inconsistencies such as a task without a record), plus
  an `autostart` block in `--json` output.
- Task definition XML with logon trigger, `HighestAvailable`, and a two-minute
  execution limit; task command arguments are quoted and escaped.
- 65 new tests (200 total): auto-start preflight and path-trust rules, task XML,
  install/remove/verify, last-run recording, `schtasks` XML parsing, and the CLI
  flows including refusal without a terminal.

### Changed

- State now includes `autostart.json`; document I/O in the store is generic over
  its kind.
- An install that cannot create the task reports the specific blocking reason in
  the error itself, because a `--json` caller never sees the printed plan.
- `status` still works when the system has no Task Scheduler: the section says so
  instead of the command failing.

### Known limitations

- The Windows API paths have run only on a GitHub-hosted Windows virtual machine
  (CI), where read, apply, verify, and restore all succeeded; they have not been
  verified on physical Windows hardware, and the resulting values have not been
  inspected by a human. See the verification table in `docs/SPEC.md`.
- `schtasks.exe` create/query/delete and the Task Scheduler XML schema have not
  been exercised on Windows at all; the XML is tested for content and
  well-formedness only.
- No GUI and no system service.
- Fan speed and fan curves are out of scope; only processor power settings are
  managed.
- Windows 8.1 and older are unsupported and untested.

## [0.1.0] — 2026-10-29

First implementation. The repository previously contained documentation only.

### Added

- Command line interface: `status`, `profiles`, `plan`, `apply`, `restore`,
  `profile set|show|delete`, with `--simulate`, `--state-dir`, `--json`,
  `--no-color`, `--preview`/`--dry-run`, and `--scope ac|dc|both`.
- Built-in profiles `quiet` (60% maximum, boost off), `balanced` (85%, boost on),
  `custom`, and `restore`, plus saved profiles that override only the values you
  name.
- Windows integration through `powrprof.dll` (`PowerGetActiveScheme`,
  `PowerRead/WriteAC/DCValueIndex`, `PowerReadFriendlyName`) with explicit
  `ctypes` structures and per-call error reporting.
- Safety guarantees: plan shown before every write, confirmation required unless
  `--yes`, baseline captured once and never overwritten, each write read back and
  verified, reverse-order rollback on failure, refusal when settings or the active
  power plan change during confirmation, and restore that keeps the recorded
  original values.
- Drift reporting: `status` shows managed values that something else has changed,
  instead of silently re-applying.
- Versioned, atomically written local state (`baseline.json`, `state.json`,
  `profiles.json`) with schema guards and read-back verification.
- Simulation mode with a persistent in-memory PC, usable on any operating system.
- Explanations for every change, a turbo-boost note for any cap below 100%, and a
  warning below 30% where some processors stutter rather than slow down.
- Test suite of 135 tests (no third-party dependencies) covering policy, planning,
  storage, controller ordering and rollback, the CLI, and GUID packing.
- Documentation: `docs/SPEC.md` (behaviour contract, supported versions, controls,
  verification ledger) and `docs/DECISIONS.md` (design decisions and rejected
  alternatives).
- GitHub Actions CI for Linux (Python 3.9, 3.11, 3.13) and an informational
  Windows job that collects real `powrprof.dll` evidence.

### Known limitations

- The Windows API paths have run only on a GitHub-hosted Windows virtual machine
  (CI), where read, apply, verify, and restore all succeeded; they have not been
  verified on physical Windows hardware, and the resulting values have not been
  inspected by a human. See the verification table in `docs/SPEC.md`.
- No GUI, no system service, and no apply-at-login (designed, not built).
- Fan speed and fan curves are out of scope; only processor power settings are
  managed.
- Windows 8.1 and older are unsupported and untested.

[0.3.1]: https://github.com/SeanShaffer88/QuietGovernor/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/SeanShaffer88/QuietGovernor/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/SeanShaffer88/QuietGovernor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SeanShaffer88/QuietGovernor/releases/tag/v0.1.0
[Unreleased]: https://github.com/SeanShaffer88/QuietGovernor/compare/v0.3.1...HEAD
