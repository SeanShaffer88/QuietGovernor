# Design decisions

Each entry records the choice, the reason, and what was rejected. Revisit only
with a reason that beats the one written here.

---

## 1. Python instead of Rust

**Decision:** implement 0.1.0 in Python 3.9+ with the standard library only.

**Why:** the product is three `PowerWrite*ValueIndex` calls wrapped in careful
bookkeeping. That bookkeeping — baseline capture ordering, confirmation,
verification, rollback, drift, versioned storage — is where all the risk lives,
and it is testable without Windows. Python's `ctypes` binds `powrprof.dll`
directly with no toolchain, and the project's standing default (Rust for new
components) is a preference to weigh, not a mandate; a native toolchain is not
available in this development environment, while the standard library is.

**What would change this:** shipping a distributable to non-technical users, where
a signed single-binary build matters more than development speed. A future
milestone may port the same modules to Rust behind the same contract; the
behaviour contract in `docs/SPEC.md` is written to survive that change.

**Rejected:** a PowerShell script (hard to test, weak error handling, poor atomic
write story), a C++ GUI (slower to reach a verifiable state), an AutoHotkey tray
tool (no test surface).

## 2. Direct `powrprof.dll` calls instead of `powercfg.exe`

**Decision:** call `PowerGetActiveScheme`, `PowerRead/WriteAC/DCValueIndex`, and
`PowerReadFriendlyName`.

**Why:** `powercfg` output is localised and has changed wording between Windows
versions; parsing it would break on non-English PCs and on unknown builds, and
would make "did the write actually take" depend on text matching. The C API also
reports a status code per call, which is what the verification and rollback
design depends on.

**Cost:** `ctypes` structure layout has to be exact. A `GUID` is 16 bytes with
`Data1`/`Data2`/`Data3` in little-endian order; the module asserts its size and
tests byte order against `uuid`.

**Rejected:** registry writes under
`HKLM\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes` (undocumented
internals: the documented API is right there), `powercfg /setacvalueindex` via
subprocess (localisation, plus a console window flash), WMI (no power-setting
write surface).

## 3. Change the active power plan; do not create a custom plan

**Decision:** write into whichever plan the user has active.

**Why:** creating a duplicated plan would change the user's active plan choice,
leave a leftover plan in Power Options after uninstall, and complicate restore
("which plan was original?"). Writing into the active plan keeps the user's
choice theirs.

**Cost:** a user switching plans afterwards is no longer governed. That is why
`status` reports drift and why the baseline records which plan it came from.

**Rejected:** a dedicated "QuietGovernor" plan (visible clutter, harder cleanup),
switching the active plan to Windows' built-in Power Saver (changes something the
user did not ask about).

## 4. Baseline is captured once and never overwritten

**Decision:** `baseline.json` is written before the first ever change and is then
immutable in normal operation. `restore` does not delete it.

**Why:** the classic failure mode of tuning tools is that "restore" restores the
tool's own last state. Capturing once, keeping forever, gives one truthful answer
to "what was it before you touched it?" — including months later, after several
profile changes.

**Cost:** a user who changes their power settings by hand after the first apply
still restores to the older values. Acceptable: the file records a time and the
plan name, and the README says so.

**Rejected:** capturing on every apply (silently loses the original), deleting the
baseline on restore (loses the ability to restore again; also makes
"re-apply after restore" irreversible).

## 5. Confirmation before the baseline, write immediately after

**Decision:** order is plan → confirm → re-read → baseline → write.

**Why:** nothing observable should happen when the user says no, including
creating files. And the baseline must be taken as late as possible before the
first write, after a fresh read, so it cannot record values that a parallel change
already invalidated.

**Consequence:** a apply that fails its first write still leaves a baseline file.
That is intentional — it is the evidence needed to undo, and deleting it would
make recovery impossible.

## 6. Only three settings, with tight ranges

**Decision:** maximum state, minimum state, boost mode. Maximum 20–100, minimum
0–50, boost 0–5. A maximum below 30% warns.

**Why:** these three are what controls heat and fan noise on mainstream hardware.
Lower maxima (under ~30%) approach the point where Windows exposes very few
performance states and some CPUs stutter instead of slowing cleanly. Capping the
minimum at 50% prevents the tool from doing the opposite of its job.

**Rejected:** processor performance increase/decrease thresholds, core parking,
energy performance preference (`PERFEPP`), C-state limits. All are real knobs, but
their effect is CPU-generation-specific and hard to explain honestly in 0.1.0.

## 7. No service, no login auto-start, no elevation shim

**Decision:** 0.1.0 does not apply itself automatically at login.

**Why:** writing power settings needs administrator rights. The options are a
UAC prompt at every login (poor, and silent failure if dismissed), or a scheduled
task running with highest privileges (works, but it is a persistent elevated
component that a tool of this kind should install only with explicit, reversible
consent — and it cannot be verified in this environment).

**Planned approach when it is built:** an opt-in `install-autostart` command
creating a Task Scheduler entry with a visible, documented name, removable with
`remove-autostart`, plus a `status` line stating whether it exists. Until then the
README and SPEC both say plainly that it does not exist.

**Rejected:** a Windows service (heavy, always-on), a startup shortcut with a
UAC prompt (the worst of both).

## 8. Local JSON files instead of SQLite or the registry

**Decision:** three small JSON documents in `%LOCALAPPDATA%\QuietGovernor`, each
with a `kind` and `schema_version`, written atomically.

**Why:** the data is tiny, human-inspectable, easy to back up or delete, and never
needed concurrently. A user can read `baseline.json` and see exactly which values
will be restored — that transparency is part of the safety story. Atomic replace
plus a read-back check covers the durability requirement that a database would
otherwise provide.

**Rejected:** the registry (opaque, easy to orphan, hard to back up), SQLite (a
dependency and an opaque file for three records), a single monolithic state file
(a corrupt file would lose the baseline too).

## 9. Simulation as a first-class mode, not a test fixture

**Decision:** `--simulate` ships in the CLI, with its own persistent state.

**Why:** it lets anyone evaluate the whole product — plans, warnings, restore,
drift — without changing a setting, on any OS. It also means the safety-critical
code paths are exercised by users, not just by tests. The simulated machine
remembers its own values so `apply` → `status` → `restore` behaves like reality.

**Rejected:** mocks hidden in tests only (no user-facing way to try it).

## 10. Command line first

**Decision:** 0.1.0 is a CLI. Output is plain, one fact per line, with colour used
only as decoration.

**Why:** the smallest surface that can satisfy the accessibility and verification
requirements, and the fastest path to something a careful user can audit. A GUI is
a later milestone that should not gate the logic being correct.

**Rejected:** a tray application first (unverifiable UI work before the engine is
proven).

## 11. Error messages carry the fix path

**Decision:** every user-facing failure states what happened, what it means, and
what to do next; state-file errors name the file and say whether deleting it is
safe.

**Why:** the project standard requires specific errors with a fix path, and this
tool touches system settings — a vague failure invites risky guessing.

## 12. Verification labels are part of the product

**Decision:** README and `docs/SPEC.md` carry a table of Verified / Partially
verified / Unverified claims, and the Windows API paths stay Unverified until a
hardware run is recorded.

**Why:** it is easy to assume `ctypes` bindings work. They are only correct if the
structure layout, argument types, and error codes were observed on the target.
Claiming otherwise would violate the project standard more seriously than shipping
less.
