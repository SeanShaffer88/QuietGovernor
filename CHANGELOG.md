# Changelog

All notable changes to QuietGovernor are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[0.2.0]: https://github.com/SeanShaffer88/QuietGovernor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SeanShaffer88/QuietGovernor/releases/tag/v0.1.0
[Unreleased]: https://github.com/SeanShaffer88/QuietGovernor/compare/v0.2.0...HEAD
