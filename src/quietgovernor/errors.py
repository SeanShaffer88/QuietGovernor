"""Error types.

Every error message in QuietGovernor is written to state three things:
what happened, why it matters, and what the user can do next. The CLI
prints ``str(error)`` verbatim, so messages must be complete sentences
that stand alone.
"""

from __future__ import annotations


class QuietGovernorError(Exception):
    """Base class for every expected QuietGovernor failure."""


class PolicyError(QuietGovernorError, ValueError):
    """A profile, setting value, or scope is not acceptable."""


class StoreError(QuietGovernorError, RuntimeError):
    """A state file could not be read, parsed, migrated, or written."""


class BackendError(QuietGovernorError, RuntimeError):
    """The power-plan backend could not read or write a setting."""


class NotElevatedError(BackendError):
    """Changing power settings requires an elevated process."""


class ActionRefused(QuietGovernorError):
    """The user (or the absence of confirmation) prevented an action."""


class ApplyFailed(BackendError):
    """A write failed after changing at least one setting."""

    def __init__(self, message: str, *, rolled_back: bool) -> None:
        super().__init__(message)
        self.rolled_back = rolled_back
