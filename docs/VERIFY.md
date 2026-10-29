# Verifying QuietGovernor on a Windows PC

`docs/SPEC.md` §9 records which claims are verified. The Windows API paths are
marked **Partially verified** because they have only ever run on a virtual
machine, and nobody has looked at the resulting values or heard a fan. This page
is how that gap gets closed: run one script on the target PC, read the report,
paste one row into the spec.

The script is `scripts/verify_windows.py`. It is read-only unless you pass
`--apply`. If you installed from a wheel, confirm which build you are testing with
`quietgovernor --version` and paste that version into the report row.

## Before you start

- A Windows 10 (1809+) or Windows 11 PC you are willing to change for about two
  minutes. Everything is put back automatically.
- Python 3.9+ and QuietGovernor installed for that Python. The package is not on
  PyPI yet, so `pip install quietgovernor` will not find it — download the wheel
  from the [Releases page](https://github.com/SeanShaffer88/QuietGovernor/releases)
  and install that file:

  ```pwsh
  py -m pip install .\quietgovernor-0.3.1-py3-none-any.whl
  ```

  The script itself ships with the source, not the wheel, so also keep a copy of
  the repository (the sdist on the same Releases page unpacks to the same files).
- An **elevated** terminal for the write phase: right-click *Windows Terminal* or
  *Command Prompt* → *Run as administrator*.

## Step 1 — read-only checks (safe)

```pwsh
py scripts/verify_windows.py
```

This records the Windows build, the active power plan, and every setting
QuietGovernor manages, and confirms that `status` reports each one sensibly. It
changes nothing. Useful on a machine you do not want touched, and on a first run.

## Step 2 — full run, including the write and sign-in paths

From an **elevated** terminal, in the repository checkout:

```pwsh
py scripts/verify_windows.py --apply --autostart
```

What it does, in order:

1. Records the environment (Windows build, elevation, active plan).
2. Reads the current values and asks QuietGovernor what it *would* change — the
   harness compares against the product's own plan, so it cannot drift.
3. Applies the `quiet` profile, then reads every value back and compares it with
   the plan.
4. Checks that the maximum state actually went *down* (a cap of 100% would mean
   nothing happened).
5. Restores the recorded original values, reads them back, and confirms they
   match what was there before this run.
6. Plans the sign-in task, reports the privilege guard's verdict, then creates the
   task, reads it back from Task Scheduler, and removes it.
7. Writes `verification-<timestamp>.json` and `.md` next to where you ran it.

Step 5 runs in a `finally` block: even if a check fails or the script is
interrupted, it attempts the restore. If the restore itself fails, the report says
so and points at the restore point, which lives in the normal per-user state
folder so it survives.

Press `Ctrl+C` and it still tries to restore. If the machine dies mid-run, run
`quietgovernor restore` — the baseline was recorded before the first write.

## Step 3 — look at Windows itself, not just the tool

The tool agreeing with itself is not enough. While the profile is applied, open
*Control Panel → Power Options → Change plan settings → Change advanced power
settings → Processor power management* and check that **Maximum processor state**
shows 60% and **Processor performance boost mode** is **Disabled**. Then run
`quietgovernor restore` and confirm they are back to their original values.

If the script's step 3 says the values changed but Windows shows them unchanged,
that is a real finding — record it.

## Step 4 — record the result

The report ends with a row ready to paste into the verification table in
`docs/SPEC.md` §9, for example:

```markdown
| Windows 11 build 26100, Python 3.12.4, elevated | 0 failed check(s), 0 phase(s) skipped | 2026-10-29T14:02:11+00:00 |
```

Change the label from **Partially verified** to **Verified** only when the run
covered everything you are claiming. Keep the report file with the pull request or
issue so the claim has evidence. If any check failed, the honest entry is
**Partially verified** plus the failure.

## What still needs a human ear

No script can tell you whether the machine is quieter. Worth recording alongside
the report, on a laptop:

- Before: fan audible at idle after a few minutes of a video call? Note it.
- After applying `quiet`: same test, same duration. Note it.
- Any stutter or dropped frames during the call. This is the cost of a low cap,
  and it is the reason `quiet` stops at 60% rather than 30%.
- Whether a vendor utility (Lenovo Vantage, Dell Power Manager, Armoury Crate,
  MSI Center) reset the values after a reboot. If it did, note the vendor tool and
  whether `autostart install` recovered it.

## Interpreting results

| Symptom | Meaning |
| --- | --- |
| `write phase can run` fails: not elevated | Re-run the whole command from an elevated terminal; nothing was changed. |
| `written values match the plan` fails | Windows kept a different value. Real bug or a vendor tool fighting for the setting. Keep the report. |
| `original values are back` fails | **Stop.** Run `quietgovernor restore` again and compare with Windows' Power Options. Report the values. |
| `restore ran` fails | The restore itself was refused. Your baseline is safe in the state folder; run `quietgovernor restore` from an elevated terminal. |
| `the privilege guard gives a verdict` fails | The preview printed neither verdict. Report the full preview text. |
| `the privilege guard gives a verdict` reports `refused` or `unknown` | **Not a failure.** The guard declined because the interpreter or package sits somewhere your account can write (typical for a per-user Python), or it could not tell and refused to guess. The report marks the task phase as skipped. |
| `sign-in task installed` fails | Windows refused the task. The message says why. |
| `the task runs elevated at sign-in` fails | The task exists but Task Scheduler reports a different command or run level. Keep the report. |

## Exercising the task despite the guard

On a throw-away machine — a VM, or CI — where Python lives somewhere
user-writable, the guard will decline the sign-in task. To exercise Task
Scheduler and the task XML anyway:

```pwsh
py scripts/verify_windows.py --apply --autostart --autostart-anyway
```

That creates a task that runs code from a user-writable folder as administrator,
then removes it at the end of the run. It is recorded in the report as an
override, and it must not be done on a machine you care about.

## For a second machine

Run the same commands. Useful coverage: one Intel hybrid CPU (P/E cores), one AMD
CPU, and one machine where `boost_mode` does not exist — on those, the run should
pass with the boost setting reported as unavailable and skipped.
