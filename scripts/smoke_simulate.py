"""End-to-end smoke test: the installed console script in simulation mode.

Runs the same apply → status → restore loop a user would run, against a
throw-away state directory, and checks the JSON reports. CI runs this after
``pip install .`` so packaging problems (missing entry point, missing modules,
broken import path) fail the build instead of surfacing for a user.

Works on Linux, macOS, and Windows: if the ``quietgovernor`` console script is
on PATH it is used; otherwise the module form is used.

Exits 0 when every expectation holds, 1 with one line per failed check.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def program() -> list[str]:
    """The command that starts QuietGovernor."""
    script = shutil.which("quietgovernor")
    if script:
        return [script]
    print("note: 'quietgovernor' is not on PATH; using 'python -m quietgovernor'")
    return [sys.executable, "-m", "quietgovernor"]


def run(command: list[str], state_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one CLI command with an isolated state directory."""
    env = dict(os.environ)
    env["QUIETGOVERNOR_STATE_DIR"] = str(state_dir)
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [*command, *arguments],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def check_json(command: list[str], state_dir: Path, *arguments: str) -> dict:
    result = run(command, state_dir, *arguments)
    label = " ".join(arguments)
    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} did not print JSON ({exc}): {result.stdout[:300]!r}") from exc


def check_exit(command: list[str], state_dir: Path, *arguments: str, failures: list[str]) -> None:
    result = run(command, state_dir, *arguments)
    if result.returncode != 0:
        failures.append(
            f"{' '.join(arguments)} exited {result.returncode}: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def main() -> int:
    command = program()
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state"

        start = check_json(command, state_dir, "--simulate", "--json", "status")
        if start["governing"]:
            failures.append("a fresh simulated PC should not be governed")
        if start["settings"]["max_state"]["ac"] != 100:
            failures.append("a fresh simulated PC should start at 100% maximum state")

        check_exit(command, state_dir, "--simulate", "apply", "--profile", "quiet", "--yes", failures=failures)
        applied = check_json(command, state_dir, "--simulate", "--json", "status")

        if not applied["governing"] or applied["profile"] != "quiet":
            failures.append("apply did not leave the PC governed by 'quiet'")
        if applied["settings"]["max_state"]["ac"] != 60:
            failures.append(f"max_state AC is {applied['settings']['max_state']['ac']}, expected 60")
        if applied["settings"]["max_state"]["dc"] != 60:
            failures.append(f"max_state DC is {applied['settings']['max_state']['dc']}, expected 60")
        if applied["settings"]["boost_mode"]["ac"] != 0:
            failures.append(f"boost_mode AC is {applied['settings']['boost_mode']['ac']}, expected 0")
        if applied["restore_point"] is None:
            failures.append("apply recorded no restore point")
        if applied["drift"]:
            failures.append(f"unexpected drift straight after apply: {applied['drift']}")

        check_exit(command, state_dir, "--simulate", "restore", "--yes", failures=failures)
        restored = check_json(command, state_dir, "--simulate", "--json", "status")

        if restored["governing"]:
            failures.append("restore left the PC governed")
        if restored["settings"]["max_state"]["ac"] != 100:
            failures.append(
                f"restore left max_state AC at {restored['settings']['max_state']['ac']}, expected 100"
            )
        if restored["settings"]["boost_mode"]["ac"] != 1:
            failures.append(
                f"restore left boost_mode AC at {restored['settings']['boost_mode']['ac']}, expected 1"
            )
        if restored["restore_point"] is None:
            failures.append("restore discarded the restore point")

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("Smoke test passed: apply changed the values, restore put them back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
