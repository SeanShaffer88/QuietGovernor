"""Shared test helpers."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class TempDirTestCase(unittest.TestCase):
    """A test case with a throw-away state directory and clean environment."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_dir = self.tmp_path / "state"
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("QUIETGOVERNOR_STATE_DIR", "LOCALAPPDATA", "NO_COLOR")
        }
        os.environ["QUIETGOVERNOR_STATE_DIR"] = str(self.state_dir)
        os.environ.pop("LOCALAPPDATA", None)
        os.environ["NO_COLOR"] = "1"

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def captured_output():
    """Capture stdout and stderr as text."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class TtyStdin(io.StringIO):
    """A stdin stand-in that claims to be a terminal."""

    def isatty(self) -> bool:  # noqa: D102 - test double
        return True


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI in-process and return (exit code, stdout, stderr)."""
    from quietgovernor.cli import main

    with captured_output() as (out, err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()
