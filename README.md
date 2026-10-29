# QuietGovernor

**Keep a Windows PC quiet by capping how hard the processor is allowed to work — and put everything back exactly as it was, whenever you want.**

QuietGovernor changes three Windows processor power settings: the maximum and
minimum processor state, and the processor performance boost mode. Capping the
maximum and turning boost off is what stops a laptop fan from spinning up during
video calls, builds, and browser tabs. Everything it touches is recorded first,
previewed before it happens, verified afterwards, and reversible with one
command.

**Status: early implementation (0.1.0).** Working code, tested logic, no release
build yet. Read [Verification status](#verification-status) before you rely on it
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

## Try it without changing anything

QuietGovernor can run against an in-memory PC. This exercises the real code —
profiles, planning, storage, restore, drift detection — and touches nothing:

```console
$ pip install quietgovernor        # or: pipx install quietgovernor
$ quietgovernor --simulate status
$ quietgovernor --simulate plan --profile quiet
$ quietgovernor --simulate apply --profile quiet --yes
$ quietgovernor --simulate restore --yes
```

Simulation state lives in `<state folder>/simulated`, separate from real state.
It is the supported way to try QuietGovernor on macOS, Linux, or a PC you would
rather not change yet.

## Requirements

| Requirement | Detail |
| --- | --- |
| Windows | Windows 10 (1809 or later) or Windows 11 are the supported targets. The power API itself dates back to Windows Vista; older versions are untested and unsupported. |
| Python | 3.9 or newer, 64-bit or 32-bit. `pipx install quietgovernor` keeps it isolated. |
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
| `restore` | Write the recorded original values back | Yes |
| `restore --preview` | Show what restore would change | No |
| `profile set NAME --max-state 70` | Save your own profile | No |
| `profile show NAME` / `profile delete NAME` | Inspect or remove a saved profile | No |

Global options: `--simulate`, `--state-dir DIR`, `--json`, `--no-color`,
`--version`. They work before or after the command name. `--yes` skips the
confirmation question but never skips the plan. Without a terminal, nothing is
written unless `--yes` is passed.

`--json` makes `status`, `plan`, `apply`, and `restore` machine-readable and
requires `--yes` for the writing commands.

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
- **No background service or login auto-start.** Applying settings requires
  administrator rights, and a silent elevated helper is exactly the kind of
  component this tool refuses to install. The intended opt-in approach (a
  scheduled task you can inspect and remove) is designed but not built yet; see
  [docs/DECISIONS.md](docs/DECISIONS.md).
- **No telemetry, no network access at all.** Nothing leaves the machine.
- **No GUI yet.** The command line is the interface in 0.1.0.

## Troubleshooting

**"Changing power settings needs administrator rights."**
Open an elevated terminal — right-click *Windows Terminal* or *Command Prompt*
and choose *Run as administrator* — then run the command again. Nothing was
changed by the failed attempt.

**"This PC does not report the processor performance boost mode setting."**
Normal on many desktops, virtual machines, and CPUs without turbo. That one
setting is skipped and everything else still applies.

**The profile seems to be ignored after a reboot.**
Vendor power software usually rewrote the values. Either turn off the vendor's
automatic power mode switching, or re-run `quietgovernor apply`. `status` will
show the drift rather than pretending nothing happened.

**The PC feels sluggish.**
Raise the cap or use `balanced`: `quietgovernor apply --profile balanced`. Caps
close to the floor (under 30%) warn before they apply, because some processors
stutter at those levels instead of merely running slowly.

## Uninstall / full removal

```console
quietgovernor restore --preview   # check what would be put back
quietgovernor restore             # put your original settings back
pipx uninstall quietgovernor      # or: pip uninstall quietgovernor
```

Then delete `%LOCALAPPDATA%\QuietGovernor`. Deleting the folder does not change
any Windows setting — only `restore` does.

## Verification status

QuietGovernor labels every claim. The platform-independent logic is verified by an
automated test suite of 135 tests that runs anywhere. The Windows API path has been
exercised once, on a GitHub-hosted Windows virtual machine (see below), but never
on physical hardware and never with the resulting values inspected by a human.

| Area | Status | Evidence |
| --- | --- | --- |
| Profiles, ranges, scope, explanations | **Verified** | `tests/test_policy.py`, `tests/test_planning.py` |
| Planning, warnings, notes, drift maths | **Verified** | `tests/test_planning.py`, `tests/test_controller.py` |
| Atomic storage, schema guards, baseline-once | **Verified** | `tests/test_store.py` |
| Apply/restore/rollback/refusal ordering | **Verified** | `tests/test_controller.py` (38 tests, simulated backend) |
| CLI behaviour, exit codes, `--json`, consent | **Verified** | `tests/test_cli.py` (25 tests) |
| GUID packing for `powrprof.dll` | **Verified** (byte layout vs `uuid`) | `tests/test_windows.py` |
| Reading real settings on Windows | **Partially verified** | Windows-only tests in `tests/test_windows.py` and a real `status` call pass on a GitHub-hosted Windows VM (CI run 40810831546) |
| Writing real settings on Windows | **Partially verified** | CI `apply` on that VM exits 0, which requires every write to be read back and match, then `restore` returns the values; the values themselves were not inspected |
| Elevation detection (`IsUserAnAdmin`) | **Partially verified** | Exercised by the CI run; only the elevated case is covered |
| Physical Windows 10/11 desktop, laptop fan behaviour, vendor software conflicts | **Unverified** | No hardware run; the checklist is in `docs/SPEC.md` §9 |

The test suite is the honest part: "Verified" above means a check ran in this
repository and passed, and nothing else.

## Development

```console
python -m unittest discover -s . -p "test_*.py"    # 135 tests, no dependencies
python -m compileall -q src tests
python -m quietgovernor --simulate status          # run from a checkout: PYTHONPATH=src
```

Layout: `policy` (what is allowed) → `planning` (what would happen) →
`store` (durable state) → `controller` (apply/restore guarantees) →
`windows` (ctypes bindings) / `backend` (protocol + simulator) → `cli`.
Design notes and rejected alternatives are in
[docs/DECISIONS.md](docs/DECISIONS.md); the behaviour specification is
[docs/SPEC.md](docs/SPEC.md).

## License

MIT. See [LICENSE](LICENSE).
