# QuietGovernor

**Keep a Windows PC quiet by capping how hard the processor is allowed to work — and put everything back exactly as it was, whenever you want.**

QuietGovernor changes three Windows processor power settings: the maximum and
minimum processor state, and the processor performance boost mode. Capping the
maximum and turning boost off is what stops a laptop fan from spinning up during
video calls, builds, and browser tabs. Everything it touches is recorded first,
previewed before it happens, verified afterwards, and reversible with one
command.

**Status: early implementation (0.3.0).** Working code, tested logic, first
release build. Read [Verification status](#verification-status) before you rely on it
— in particular, the Windows API path has run only on a virtual CI machine, never
on physical hardware.

---

## What it does, in plain terms

Windows already has these settings, buried under *Control Panel → Power Options
→ Change plan settings → Change advanced power settings → Processor power
management*. QuietGovernor gives you three things Windows does not:

1. **A preview with an explanation.** You see every value that will change, what
   it means, and what it costs you (for example: turbo boost is turned off, so
   short bursts are slower).
2. **A guaranteed way back.** The very first change records your original
   values in a file, and `quietgovernor restore` writes them back — even months
   later, even if you have switched power plans since.
3. **Honesty about the result.** Every write is read back from Windows and
   compared. If a value did not stick, or Windows reported something different,
   the change is rolled back and you are told exactly what happened.

It can also, if you ask it to, re-apply your profile at sign-in — because Windows
and vendor utilities tend to reset these values. That is a deliberate,
separately-consented step: a scheduled task that runs with administrator rights,
inspected and removable, with no service and no background process.

## Try it without changing anything

QuietGovernor can run against an in-memory PC. This exercises the real code —
profiles, planning, storage, restore, drift detection — and touches nothing:

```console
$ pip install ./quietgovernor-0.3.0-py3-none-any.whl   # see Installing
$ quietgovernor --simulate status
$ quietgovernor --simulate plan --profile quiet
$ quietgovernor --simulate apply --profile quiet --yes
$ quietgovernor --simulate autostart install --profile quiet --yes
$ quietgovernor --simulate restore --yes
```

Simulation state lives in `<state folder>/simulated`, separate from real state.
It is the supported way to try QuietGovernor on macOS, Linux, or a PC you would
rather not change yet.

## Installing

```console
# from the wheel attached to a release (Releases page), isolated, recommended
pipx install ./quietgovernor-0.3.0-py3-none-any.whl

# or into a Python you manage
py -m pip install quietgovernor-0.3.0-py3-none-any.whl

# or straight from a checkout
py -m pip install .
```

QuietGovernor is **not on PyPI yet**, so `pip install quietgovernor` will not find
it — downloads come from the Releases page. Check what you installed with
`quietgovernor --version`.

Prefer installing Python **for all users** if you plan to use the sign-in task:
the privilege guard in [Optional: re-apply at sign-in](#optional-re-apply-at-sign-in)
refuses a per-user Python, for good reason.

## Requirements

| Requirement | Detail |
| --- | --- |
| Windows | Windows 10 (1809 or later) or Windows 11 are the supported targets. The power API itself dates back to Windows Vista; older versions are untested and unsupported. |
| Python | 3.9 or newer, 64-bit or 32-bit. `pipx install ./quietgovernor-0.3.0-py3-none-any.whl` keeps it isolated. |
| Rights | **Administrator.** Windows refuses power-setting writes to standard users. Reading and previewing work without it. |
| Firmware | Modern standby or S3 sleep, either is fine. No driver, service, or kernel component is installed. |

One caveat worth knowing before you start: on some laptops the vendor's own
control software (Lenovo Vantage, Dell Power Manager, Armoury Crate, MSI Center)
also writes these settings and can overwrite them on reboot or on a fan-mode
change. See [Troubleshooting](#troubleshooting).

## Quick start

```console
# 1. Look at the machine. Changes nothing.
quietgovernor status

# 2. See exactly what the 'quiet' profile would do, and why.
quietgovernor plan --profile quiet

# 3. Apply it from an elevated terminal. The plan is shown again and you confirm it.
quietgovernor apply --profile quiet

# 4. Later: put everything back exactly as it was.
quietgovernor restore
```

## Profiles

| Profile | Maximum | Minimum | Boost | Best for |
| --- | --- | --- | --- | --- |
| `quiet` | 60% | 0% | off | Writing, browsing, calls, a fan you can hear from across the room |
| `balanced` | 85% | 0% | on | Everyday use with a gentler ceiling |
| `custom` | 100% | 0% | on | A starting point you edit with `profile set` |
| `restore` | — | — | — | Puts your recorded original values back |

Make your own. A saved profile changes **only** the values you name; everything
else stays exactly as it is:

```console
quietgovernor profile set meetings --max-state 70 --boost off --description "Quiet calls"
quietgovernor plan --profile meetings
quietgovernor apply --profile meetings
```

Names are simple on purpose — letters, digits, dots, dashes, underscores, up to 40
characters — because they appear on the command line and in the state file. The
built-in names (`quiet`, `balanced`, `custom`, `restore`) are reserved: saving over
one would hide it everywhere and could never be undone, so `profile set quiet ...`
is refused with a suggested name like `quiet-mine`.

`--scope ac` limits a profile to mains power, `--scope dc` to battery, and the
default `both` covers each. `--min-state` is capped at 50% on purpose: raising
it keeps clocks high, which is louder — the opposite of what this tool is for.

## Commands

| Command | What it does | Changes settings? |
| --- | --- | --- |
| `status` | Active plan, current values, what is managed, any drift, restore point | No |
| `profiles` | List available profiles and what they set | No |
| `plan --profile NAME` | Show the exact plan, explanations, and warnings | No |
| `apply --profile NAME` | Show the plan, ask, then write and verify | Yes |
| `apply --profile NAME --preview` | Same as `plan`, for muscle memory | No |
| `restore` | Write the recorded original values back, in the plan they came from | Yes |
| `restore --into-active-plan` | …into the plan that is active now, leaving the other plan alone | Yes |
| `restore --preview` | Show what restore would change | No |
| `profile set NAME --max-state 70` | Save your own profile | No |
| `profile show NAME` / `profile delete NAME` | Inspect or remove a saved profile | No |
| `autostart install --profile NAME` | Register an optional sign-in task that re-applies a profile | Not power settings; adds a task |
| `autostart remove` | Delete that sign-in task | Removes a task only |
| `autostart run` | Apply a profile without asking (this is what the task runs) | Yes |
| `autostart install --preview` / `remove --preview` | Show what would change | No |

Global options: `--simulate`, `--state-dir DIR`, `--json`, `--no-color`,
`--version`. They work before or after the command name. `--yes` skips the
confirmation question but never skips the plan. Without a terminal, nothing is
written unless `--yes` is passed — and the command then exits **1**, so a script
or scheduled task can tell that it did not apply anything.

`--json` makes `status`, `plan`, `apply`, and `restore` machine-readable and
requires `--yes` for the writing commands; without it they exit 1 rather than
pretending to have acted.

## Optional: re-apply at sign-in

Windows and vendor utilities often reset these values after a reboot, so a quiet
laptop becomes noisy again. QuietGovernor can fix that automatically:

```console
quietgovernor autostart install --profile quiet     # shows the plan, asks first
quietgovernor status                                # shows whether it exists
quietgovernor autostart remove                      # delete it again
```

What it registers: one Task Scheduler entry, `\QuietGovernor\ApplyAtLogon`, that
runs about 20 seconds after you sign in, as you, with highest available
privileges, and executes `quietgovernor autostart run --profile quiet`. No
service, no tray icon, no background process, no network access. `status` shows
the task, the profile it applies, its privileges, and the outcome of the last run
(including failures, which is the only way to notice them under `pythonw.exe`).

**Why installing it is a separate, deliberate step.** A task that runs with
administrator rights at every sign-in runs whatever program it points at. If that
program — the Python interpreter or the QuietGovernor package — lives in a folder
your normal account can write to, then anything that can write there gets
administrator code execution at your next sign-in. So QuietGovernor refuses to
install auto-start unless both live in an administrator-only folder (such as
`C:\Program Files`). A default *per-user* Python install does not qualify; the
error message tells you to install Python "for all users" instead.
`--allow-untrusted-paths` overrides the check, and then says so every time.

Removing the task never changes your processor settings, and `restore` never
touches the task. The two are independent, so you can undo one without the other.

## How it works

The three settings live in the **Processor power management** subgroup of the
active power plan, separately for mains (AC) and battery (DC) power:

| Setting | powercfg alias | What it controls |
| --- | --- | --- |
| Maximum processor state | `PROCTHROTTLEMAX` | The highest performance state the CPU may enter. Below 100% removes turbo on most CPUs — this is what makes the machine quieter. |
| Minimum processor state | `PROCTHROTTLEMIN` | The lowest state the CPU may idle down to. 0% lets it idle at its coolest. |
| Processor performance boost mode | `PERFBOOSTMODE` | Whether the CPU may exceed its rated speed. Off is cooler and quieter; some PCs do not expose this setting at all. |

QuietGovernor calls the documented Power Management API (`powrprof.dll`:
`PowerGetActiveScheme`, `PowerRead*ValueIndex`, `PowerWrite*ValueIndex`,
`PowerReadFriendlyName`) rather than parsing `powercfg.exe` text, because that
output is localised and differs between Windows versions. Plans are written into
the **currently active** plan, so your power-plan choice is never changed for
you.

## Safety model

1. **Nothing is written until you confirm.** The plan is printed first, every
   time, including with `--yes`.
2. **The baseline is captured before the first write, and only once.** It is
   stored in `baseline.json` and never overwritten, so a second `apply` cannot
   destroy the record of how your PC really was.
3. **Each write is read back.** A value that does not stick is treated as a
   failure, not a success.
4. **A failure part-way rolls back.** Changes already written are put back in
   reverse order; if even that fails, you are told which values remain changed
   and given the restore command.
5. **Stale plans are refused.** If a setting or the active power plan changes
   while the confirmation question is on screen, nothing is written.
6. **Drift is reported, not hidden.** If something else changes a managed value
   afterwards, `status` says so and does not claim to be in control.
7. **`restore` keeps the record.** It writes your original values back and stops
   managing the machine, but keeps `baseline.json` so those values can always be
   put back again.

State files live in `%LOCALAPPDATA%\QuietGovernor` (override with `--state-dir`
or `QUIETGOVERNOR_STATE_DIR`) and are written atomically: temporary file in the
same folder, flush, rename, then read back and compared. On Windows they inherit
the per-user permissions of `%LOCALAPPDATA%`; on POSIX systems they are created
owner-read/write only.

## What QuietGovernor does not do

- **No fan control.** It cannot set fan curves or read fan speeds; that needs
  vendor drivers. It reduces heat instead, and the fan follows.
- **No undervolting or overclocking.** No invalid or unsupported firmware
  settings are changed.
- **No background service or resident tray helper.** The only persistent thing
  QuietGovernor can install is the opt-in sign-in task described above, which
  runs once at sign-in and exits. Nothing stays running.
- **No telemetry, no network access at all.** Nothing leaves the machine.
- **No GUI yet.** The command line is the interface in 0.3.0.

## Troubleshooting

**"Changing power settings needs administrator rights."**
Open an elevated terminal — right-click *Windows Terminal* or *Command Prompt*
and choose *Run as administrator* — then run the command again. Nothing was
changed by the failed attempt.

**"This PC does not report the processor performance boost mode setting."**
Normal on many desktops, virtual machines, and CPUs without turbo. That one
setting is skipped and everything else still applies.

**`restore` says it will not run because a different power plan is active.**
Windows keeps these settings per plan, so the values QuietGovernor wrote live in
the plan it wrote them to; restoring into the plan you are using now would undo
nothing and quietly leave the other plan capped. Switch to the plan named in the
message and run `quietgovernor restore` again — or run
`quietgovernor restore --into-active-plan` if you want the recorded values in the
plan you are using now and do not care about the other one (the restore point is
kept either way, so you can still clear the other plan later).

**`status` says the active power plan is not the one the profile was applied to.**
You switched Windows power plans. QuietGovernor writes into the plan that is
active and does not change your plan choice, so the values it set live in the
other plan. Apply again to govern the plan you are using now; `status` will stop
saying it as soon as that plan matches.

**The profile seems to be ignored after a reboot.**
Vendor power software usually rewrote the values. Either turn off the vendor's
automatic power mode switching, re-run `quietgovernor apply`, or install the
optional sign-in task (`quietgovernor autostart install --profile quiet`) so the
profile is re-applied automatically. Either way `status` shows the drift rather
than pretending nothing happened.

**`autostart install` refuses, saying the interpreter or package is user-writable.**
That is the privilege-escalation guard. Python installed "for all users" lives in
`C:\Program Files` and qualifies; a per-user install does not. Reinstall Python
for all users, install QuietGovernor into that interpreter, and try again — or
accept the risk explicitly with `--allow-untrusted-paths`.

**The PC feels sluggish.**
Raise the cap or use `balanced`: `quietgovernor apply --profile balanced`. Caps
close to the floor (under 30%) warn before they apply, because some processors
stutter at those levels instead of merely running slowly.

## Uninstall / full removal

```console
quietgovernor autostart remove    # delete the sign-in task, if you installed one
quietgovernor restore --preview   # check what would be put back
quietgovernor restore             # put your original settings back
pipx uninstall quietgovernor      # or: pip uninstall quietgovernor
```

Then delete `%LOCALAPPDATA%\QuietGovernor`. Deleting the folder does not change
any Windows setting — only `restore` does.

## Verification status

QuietGovernor labels every claim. The platform-independent logic is verified by an
automated test suite of 261 tests that runs anywhere. The Windows API path has been
exercised once, on a GitHub-hosted Windows virtual machine (see below), but never
on physical hardware and never with the resulting values inspected by a human.

| Area | Status | Evidence |
| --- | --- | --- |
| Profiles, ranges, scope, explanations | **Verified** | `tests/test_policy.py`, `tests/test_planning.py` |
| Planning, warnings, notes, drift maths | **Verified** | `tests/test_planning.py`, `tests/test_controller.py` |
| Atomic storage, schema guards, baseline-once | **Verified** | `tests/test_store.py` |
| Apply/restore/rollback/refusal ordering | **Verified** | `tests/test_controller.py` (50 tests, simulated backend) |
| Auto-start preflight, path trust, install/remove/verify | **Verified** | `tests/test_autostart.py` (45 tests, simulated scheduler) |
| CLI behaviour, exit codes, `--json`, consent | **Verified** | `tests/test_cli.py` (60 tests) |
| GUID packing for `powrprof.dll` | **Verified** (byte layout vs `uuid`) | `tests/test_windows.py` |
| Reading real settings on Windows | **Partially verified** | Windows-only tests in `tests/test_windows.py` and a real `status` call pass on a GitHub-hosted Windows VM (CI run 40810831546) |
| Writing real settings on Windows | **Partially verified** | CI `apply` on that VM exits 0, which requires every write to be read back and match, then `restore` returns the values; the values themselves were not inspected |
| Elevation detection (`IsUserAnAdmin`) | **Partially verified** | Exercised by the CI run; only the elevated case is covered |
| `schtasks.exe` task create/query/delete, and the task XML being accepted | **Partially verified** | The verification harness creates, reads back, and removes the task on a CI Windows VM, and the job fails if any check fails (CI run 40812787671) |
| The verification harness (`scripts/verify_windows.py`) | **Verified** | 25 unit tests, run in simulation on every push and on Windows in CI |
| Physical Windows 10/11 desktop, laptop fan behaviour, vendor software conflicts | **Unverified** | No hardware run; the checklist is in `docs/SPEC.md` §9 |

The test suite is the honest part: "Verified" above means a check ran in this
repository and passed, and nothing else.

If you want to help close the gaps, [docs/VERIFY.md](docs/VERIFY.md) has the
procedure: one script, `py scripts/verify_windows.py --apply --autostart`, which
records your Windows build, applies a profile, reads every value back, restores
your originals, exercises the sign-in task, and writes a report you can attach to
an issue. It is read-only unless you pass `--apply`.

## Development

```console
python -m unittest discover -s . -p "test_*.py"    # 261 tests, no dependencies
python -m compileall -q src tests
python -m quietgovernor --simulate status          # run from a checkout: PYTHONPATH=src
```

Layout: `policy` (what is allowed) → `planning` (what would happen) →
`store` (durable state) → `controller` (apply/restore guarantees) →
`autostart` (sign-in task rules and behaviour) →
`windows` (ctypes bindings and `schtasks`) / `backend` (protocol + simulator) → `cli`.
Design notes and rejected alternatives are in
[docs/DECISIONS.md](docs/DECISIONS.md); the behaviour specification is
[docs/SPEC.md](docs/SPEC.md).

## License

MIT. See [LICENSE](LICENSE).
