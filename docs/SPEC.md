# QuietGovernor — behaviour specification (0.2.0)

This document is the first-project milestone the README promised: intended
behaviour, supported Windows versions, user controls, and a small implementation
that can be checked. It describes what version 0.2.0 **does**, not what a future
version might do. Anything not written here is out of scope.

Verification labels used throughout: **Verified** (a check in this repository ran
and passed), **Partially verified** (some checks ran; gaps named), **Unverified**
(nothing ran).

---

## 1. The problem

A laptop fan spins up when the processor holds a high performance state for a
few seconds: opening a browser tab, joining a call, starting a build. Windows can
cap that, but the controls are hidden in Power Options, the effect is
unexplained, and undoing it exactly is fiddly. Users who try it either cannot
find the setting, cannot tell what it costs them, or cannot get their original
values back afterwards.

QuietGovernor's scope: **change three processor power settings, explain them,
verify them, and be able to undo them exactly.** Optionally, re-apply one profile
at sign-in, because Windows and vendor utilities reset these values.

Fan curves, undervolting, overclocking, per-application profiles, and always-on
background components stay out of scope (see §5).

## 2. Supported environment

| Item | Supported in 0.2.0 |
| --- | --- |
| Windows 10 | Yes, 1809 or later (target) |
| Windows 11 | Yes (target) |
| Windows 8.1 and older | No. Untested, unsupported, not blocked by a version check. |
| Python | 3.9+ |
| Architecture | x86-64 and 32-bit; the API is architecture-neutral |
| Rights | Administrator for writes; reads and previews work unelevated |
| Virtual machines | Reads work; boost mode is often absent; writes may be refused by the hypervisor |
| macOS / Linux | Only `--simulate`. The real backend refuses with an explanation. |

**Verification:** the Windows API path is **Partially verified** — it ran once on a
GitHub-hosted Windows virtual machine (CI run 40810831546) with apply, read-back
and restore all succeeding. No physical Windows 10/11 hardware run has been
recorded, so fan behaviour and vendor-software conflicts remain **Unverified**.
See §9.

## 3. Behaviour contract

These are the promises the implementation must keep. Each has tests.

### 3.1 Reading

1. `status` reports the active power plan's friendly name and GUID, plus the
   value of each managed setting for mains (AC) and battery (DC) power.
2. A setting the PC does not expose is reported as unavailable, not as zero and
   not as an error. `boost_mode` is commonly absent on desktops.
3. Anything unreadable is stated plainly; no value is invented.

### 3.2 Planning

4. A plan lists every change as a sentence: setting, power source, from, to,
   and what the new value means in words.
5. Values already equal to the target are listed separately as "already at the
   requested value". A setting a saved profile does not mention is not listed at
   all, because it is not a target.
6. Any maximum below 100% adds a note explaining that turbo boost is off and
   what that costs.
7. A maximum below 30% adds a warning that some processors stutter at that level
   and suggests 40% or higher. The value is still allowed.
8. A missing required setting (maximum or minimum processor state) aborts
   planning with a message naming the setting and confirming nothing changed.
   A missing `boost_mode` is skipped with one note.

### 3.3 Applying

9. Nothing is written before the plan is shown. `--yes` skips the question, not
   the plan.
10. Without a terminal, an apply requires `--yes`; otherwise it refuses and
    writes nothing.
11. The baseline is recorded **after** confirmation and **before** the first
    write. A declined apply leaves no file behind.
12. The baseline is recorded once, ever. Later applies never overwrite it.
13. If the active power plan or any managed value changes between planning and
    writing, the apply aborts with nothing written.
14. Every write is read back. A value that does not match is a failure.
15. On failure, everything already written is restored in reverse order. If
    rollback also fails, the message names the values still changed and points at
    `restore` and the baseline file.
16. `state.json` is only written when the machine actually changed. An apply that
    found nothing to do records nothing and claims nothing.

### 3.4 Restoring

17. `restore` writes the recorded baseline values into the active plan and then
    stops managing the machine (`state.json` is removed).
18. The baseline file is kept after a restore, so the original values remain
    available.
19. `restore` with no baseline fails with "nothing to undo" and changes nothing.
20. `restore` after a power-plan switch writes the recorded values into the plan
    that is active now and says so in the plan notes.
21. `restore` refuses a stale plan exactly like `apply` (point 13).

### 3.5 Reporting

22. `status` says whether anything is managed, which profile, and when.
23. If a managed value no longer matches, `status` reports it as drift with the
    live value, the expected value, and the two ways to fix it. It never
    silently re-applies.

### 3.6 Profiles

24. Built-in: `quiet`, `balanced`, `custom`, `restore`. Built-ins cannot be
    deleted.
25. A saved profile overrides only the values it names; everything else is
    inherited from the live settings at plan time and is not written.
26. `--scope ac|dc|both` restricts which power sources a profile governs.
27. Validation: maximum 20–100%, minimum 0–50%, boost 0–5 (or the names `off`,
    `on`, `aggressive`, `efficient`, plus the two `-guaranteed` variants), and
    minimum must not exceed maximum for the same power source. Messages state the
    accepted range and what to do.

### 3.7 Sign-in auto-start (optional, off by default)

29. Auto-start is never enabled implicitly. It exists only after an explicit
    `autostart install`, and `status` states plainly whether it is present.
30. Installing requires: an existing profile, an elevated terminal (Windows will
    not otherwise accept a task that runs with highest privileges), and an
    interpreter plus package folder inside a location only administrators can
    write to. Any failure stops the install with the reason and a fix path, and
    nothing is created.
31. If the administrator-only folders cannot be determined, the install fails
    closed rather than assuming safety. `--allow-untrusted-paths` overrides the
    folder check and the override is reported in the plan, the install result,
    and `status`.
32. The created task runs at sign-in for the current user, with highest
    available privileges, ignoring battery/network/idle conditions, with a
    two-minute limit, and no console window (`pythonw.exe` where available).
33. The task is verified after creation by reading it back: if Task Scheduler
    reports no task, or a different command, the install fails and says so.
34. `autostart run` applies one profile with no confirmation (consent was given at
    install time) and records the outcome — success or the error text — where
    `status` reports it.
35. `autostart remove` deletes the task and verifies it is gone. Removing a task
    never changes power settings; `restore` never touches the task.
36. `status` reports inconsistencies rather than hiding them: a task with no
    record, a record with no task, a task that points at another program, or a
    task whose command differs from the recorded one.

### 3.8 Data

37. State lives in `%LOCALAPPDATA%\QuietGovernor` (overridable). Four files:
    `baseline.json`, `state.json`, `profiles.json`, `autostart.json`.
38. Each file is JSON with an envelope: `kind`, `schema_version` (currently 1),
    `data`. A wrong kind, a future version, or a truncated file is reported with
    the file path and a fix path; nothing is guessed.
39. Writes are atomic: temporary file in the same directory, flush, rename, then
    read back and compare.
40. On POSIX systems files are created owner-read/write only; on Windows they
    inherit the per-user permissions of `%LOCALAPPDATA%`.

## 4. User controls

| Control | Kind | Default | Notes |
| --- | --- | --- | --- |
| Profile choice | `--profile` | `quiet` | Built-in or saved |
| Power sources | `--scope` | `both` | `ac`, `dc`, `both` |
| Own values | `profile set --max-state/--min-state/--boost` | — | Saved and re-usable |
| Confirmation | interactive prompt | on | `--yes` skips the question |
| Preview | `plan`, `--preview`, `--dry-run` | — | Writes nothing |
| Reversibility | `restore` | — | Uses the recorded baseline |
| Machine-readable output | `--json` | off | Writing commands then require `--yes` |
| Colour | automatic, `--no-color`, `NO_COLOR` | auto | Never used for meaning alone |
| State location | `--state-dir`, `QUIETGOVERNOR_STATE_DIR` | `%LOCALAPPDATA%` | |
| Simulation | `--simulate` | off | Separate state folder |
| Sign-in auto-start | `autostart install/remove` | off | Requires elevation and admin-only code paths |
| Privilege guard override | `--allow-untrusted-paths` | off | Reported everywhere it is used |
| Task payload | `autostart run --profile` | — | Runs unattended, records its outcome |

Accessibility: plain line-based output, no meaning conveyed by colour alone
(every state also has words), no animation, works in a screen reader because
output is one sentence per line, exit codes documented (0 success or deliberate
refusal, 1 error, 2 usage, 130 interrupted).

## 5. Non-goals

Fan curves and fan speed control; undervolting; overclocking; per-application
power profiles; a GUI; a system service; any always-running component (the sign-in
task runs once and exits); changing the active power plan; changing sleep,
display, or battery settings; telemetry; any network access.

## 6. Implementation shape

| Module | Responsibility | Windows-specific |
| --- | --- | --- |
| `policy` | Profiles, allowed ranges, explanations, GUID constants | No |
| `planning` | Readings, baseline, plan building, warnings | No |
| `store` | Versioned documents, atomic writes, validation | No |
| `backend` | `PowerBackend` protocol, simulated PC, drift maths | No |
| `controller` | Apply/restore ordering, rollback, verification, state | No |
| `autostart` | Task definition, path trust rules, install/remove/run, last-run record | No |
| `windows` | `powrprof.dll` calls, elevation check, GUID packing, `schtasks.exe` | Yes |
| `cli` | Argument parsing, output, consent | No |

The split exists so that everything except the final API call is testable on any
operating system, and so the safety ordering has one home.

## 7. Settings written

| Setting | Subgroup GUID | Setting GUID | Range written | Reason for the range |
| --- | --- | --- | --- | --- |
| Maximum processor state | `54533251-82be-4824-96c1-47b60b740d00` | `bc5038f7-23e0-4960-96da-33abaf5935ec` | 20–100 | Below 20 Windows hides nearly every performance state |
| Minimum processor state | `54533251-82be-4824-96c1-47b60b740d00` | `893dee8e-2bef-41e0-89c6-b55d0929964c` | 0–50 | Raising it keeps clocks up: louder, opposite to the purpose |
| Processor performance boost mode | `54533251-82be-4824-96c1-47b60b740d00` | `be337238-0d82-4146-a960-4f3749d470c7` | 0–5 | Documented values only |

Both AC and DC indexes are written for each setting, unless `--scope` says
otherwise.

## 8. Acceptance criteria for this milestone

- [x] Supported Windows versions named, with the unverified ones labelled.
- [x] Behaviour contract above, with one or more tests per numbered promise.
- [x] User controls enumerated, including how to say no.
- [x] A working implementation of the above, runnable on any OS in simulation.
- [x] Documented, versioned, atomic local storage.
- [x] Reversible by design, with the restore path tested including failure paths.
- [x] Optional sign-in auto-start, off by default, with a privilege-escalation
      guard, read-back verification, and an inspectable last run.
- [ ] Real Windows hardware run recorded (see §9). A virtual-machine run now
      exists in CI and is recorded as Partially verified; physical hardware, fan
      behaviour, and vendor-software conflicts are still open. This remains the
      milestone gate.

## 9. Verification ledger

| Claim | Label | Basis |
| --- | --- | --- |
| Settings identifiers and ranges | Verified | `tests/test_policy.py` pins every GUID and bound |
| Plan text, warnings, notes | Verified | `tests/test_planning.py` |
| Baseline captured once, before first write, never on refusal | Verified | `tests/test_controller.py` |
| Rollback after a failed write restores every value | Verified | `tests/test_controller.py` (simulated failing PC) |
| Verification catches a write Windows did not apply | Verified | `tests/test_controller.py` (simulated ignoring PC) |
| Restore returns original values and keeps the record | Verified | `tests/test_controller.py` |
| Drift detection | Verified | `tests/test_controller.py`, `tests/test_cli.py` |
| Atomic writes and schema guards | Verified | `tests/test_store.py` |
| CLI consent, exit codes, `--json` purity | Verified | `tests/test_cli.py` |
| GUID packing correct for `powrprof.dll` | Verified | `tests/test_windows.py` vs `uuid` |
| `powrprof.dll` reads on real Windows | Partially verified | Windows-only tests and a real `status` call pass on a GitHub-hosted Windows VM (CI run 40810831546) |
| `powrprof.dll` writes on real Windows | Partially verified | `apply --yes` exits 0 on that VM, which requires every write to be read back and match; `restore --yes` then succeeds. Values were not inspected by a human, and no physical hardware was used. |
| Elevation detection | Partially verified | Exercised by the CI run (elevated case only) |
| Behaviour on specific laptop models / vendor software conflicts | Unverified | No hardware matrix |
| Auto-start preflight, path-trust rules, task XML, install/remove/verify | Verified | `tests/test_autostart.py` (45 tests, simulated scheduler) |
| Auto-start consent, `--json`, and refusal without a terminal | Verified | `tests/test_cli.py` |
| `schtasks.exe` on real Windows (create/query/delete a task) | Partially verified | The verification harness creates, reads back, and removes the task on a GitHub-hosted Windows VM and gates the CI job on the result (CI run 40812787671) |
| Task XML accepted by Windows Task Scheduler | Partially verified | Implied by the same run: the task could only exist if Windows accepted the XML |
| Verification harness itself | Verified | Exercised in simulation on every push, and on Windows by CI; 19 unit tests cover its verdicts and restore guarantees |
| Physical Windows 10/11 hardware, fan behaviour, vendor software conflicts | Unverified | Still needs a person and a laptop; procedure in [VERIFY.md](VERIFY.md) |
| Python 3.9, 3.11, and 3.13 compatibility | Verified | CI matrix runs the full suite on all three (CI run 40810831546) |
| Packaging: `pip install .` and the console script | Verified | CI installs the package and runs `quietgovernor --version` before the smoke test; `scripts/smoke_simulate.py` then drives apply → status → restore |

### What a hardware run must do

The CI run above covers step 2 and 3 mechanically on a virtual machine, but
nobody has looked at the resulting values, and the machine has no fan. The
following is still required on physical hardware:

1. `quietgovernor status` on stock Windows 10 and Windows 11, elevated and not.
2. `quietgovernor apply --profile quiet --yes`, then check Windows' own Power
   Options shows 60% maximum and boost off.
3. `quietgovernor restore --yes`, then check the original values are back.
4. Repeat on one Intel hybrid CPU, one AMD CPU, and one machine without boost
   mode exposed.
5. Record the output and the Windows build in this file.

Steps 1–4 are automated by `scripts/verify_windows.py`, which produces a JSON and
Markdown report (including the environment, every expected and actual value, and
a pasteable row for this table). Step 5 still needs a person. The procedure,
including what no script can check — whether the fan is actually quieter — is in
[docs/VERIFY.md](VERIFY.md).
