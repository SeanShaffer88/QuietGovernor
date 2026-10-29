# QuietGovernor — agent notes

---

## Development standard

This project follows SEAN's **Universal Windows Desktop Master Prompt** — the standing development standard for all of SEAN's Windows desktop projects, current and future. The full text is maintained by SEAN (ask him for the current version); this section is the in-repo summary, and the project's own instructions add project-specific requirements on top of it.

Core standards:

- Production quality only: no rough demos or placeholder features presented as finished; safe defaults for beginners, explained advanced controls for professionals.
- Accessibility is a requirement: keyboard navigation, accessible names, visible focus, sufficient contrast, no meaning by color alone, specific error messages with a fix path.
- Every consequential action explains its risks, limitations, and recovery options; destructive or irreversible actions are confirmed, previewed where feasible, and backed up or undoable where feasible.
- Data safety: local-first storage, offline-capable core features, versioned data formats, atomic writes, tested migrations, and no silent data loss.
- Verification honesty: compatibility and readiness claims are labeled Verified, Partially verified, or Unverified; never claim a check that did not run.

Standing defaults (a project brief may override a specific default):

- Product-family look: the black/red aesthetic matching InstallScope is preferred, not mandatory.
- Rust is the default for new projects and for new components where it fits; existing stacks are preserved — this is not a rewrite mandate.
- Support as many Windows versions as makes sense for the project; consider Windows 10 support when feasible without materially extending development time. Never claim support that has not been verified.
- Status and task reports include a Progress % and an ETA whenever they can be estimated responsibly.
- Public documentation avoids competitor comparisons by default (internal research may name competitors; license attributions for bundled components are kept).
- License: MIT for SEAN-owned projects; forks retain their upstream license.

---

## Implementation notes (0.2.0)

This section records what exists, so future work builds on it instead of
guessing. Behaviour is specified in `docs/SPEC.md`; the reasoning behind each
design choice is in `docs/DECISIONS.md`.

### Status

- Working Python 3.9+ CLI, standard library only, at `src/quietgovernor/`.
- 200 tests, run with `python -m unittest discover -s . -p "test_*.py"`.
- The Windows path (`windows.py`) is implemented but **unverified on hardware**.
  Do not describe it as working until `docs/SPEC.md` §9 records a run.

### Module boundaries (keep them)

`policy` (what is allowed) → `planning` (what would happen) → `store` (durable
state) → `controller` (apply/restore guarantees) → `windows` / `backend`
(the API boundary) → `cli`. Nothing above `windows.py` may import ctypes, and
nothing below `controller.py` may decide when to ask the user anything.

### Invariants that must not regress

Each has a test in `tests/test_controller.py`; adding a behaviour that weakens
one of these is a regression, not a trade-off.

1. A baseline is written only after confirmation and before the first write.
2. A refused or refused-by-absence-of-terminal apply writes nothing at all.
3. The first baseline is never overwritten by later applies.
4. Every write is read back; a mismatch is a failure, not a success.
5. A failed write rolls back everything already written.
6. If rollback also fails, the error names the remaining values and the restore
   command.
7. An apply that found nothing to change records no state and claims nothing.
8. `restore` keeps `baseline.json`.
9. A plan or setting change during confirmation aborts with nothing written.
10. Simulation state never mixes with real state.
11. Auto-start is never enabled implicitly: it exists only after an explicit
    `autostart install`.
12. `autostart install` refuses when the interpreter or package is writable by
    the signed-in account, or when the protected folders cannot be determined
    (fail closed). `--allow-untrusted-paths` is the only override, and every
    place that reports the task must disclose it.
13. A created task is read back from Task Scheduler and compared before the
    install is reported as successful.
14. Removing the task never changes power settings, and `restore` never touches
    the task.

### Verification discipline

- "Verified" means a test in this repository ran and passed. Windows API calls
  stay Unverified until a real run is recorded in `docs/SPEC.md` §9.
- Update the README verification table and `docs/SPEC.md` in the same change that
  alters a claim. Do not let the tables drift from the tests.
- Never add a feature that changes system settings without preview, confirmation,
  and a recorded restore path.

### Testing notes

- No third-party dependencies; use `unittest` and the helpers in `tests/__init__.py`.
- `SimulatedBackend` supports missing settings (`unavailable=`), failing writes
  (`fail_on=`), and a persisted simulated machine (`state_path=`), which is enough
  to test every failure path without Windows.
- `SimulatedScheduler` stands in for Task Scheduler, with the same create /
  describe / delete shape as the Windows implementation. Subclass it to test
  verification failures (a scheduler that ignores `create` or returns a different
  command).
- Path-trust helpers (`protected_roots`, `is_in_protected_location`) are pure and
  take the environment as an argument, deliberately not `os.path`, because they
  must give the same answer on Windows and in tests elsewhere.
- Set `QUIETGOVERNOR_STATE_DIR` to a temporary directory in tests; never write to
  a real user profile.
