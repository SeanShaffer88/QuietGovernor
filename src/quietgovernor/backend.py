"""The power-plan boundary.

Everything above this layer works against the small protocol below, so the
controller can be tested without Windows and the Windows implementation
(``quietgovernor.windows``) contains nothing but API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from .errors import BackendError
from .planning import Baseline, Reading, Readings, utc_now
from .policy import AC, DC, POWER_SOURCES, SETTINGS, WRITE_ORDER
from .store import atomic_write_text


class PowerBackend(Protocol):
    """Read and write the machine's active power plan settings."""

    def active_scheme(self) -> tuple[str, str]:
        """Return ``(guid, friendly_name)`` of the active power plan."""

    def read(self, setting_key: str) -> Reading:
        """Return the machine's current values for ``setting_key``."""

    def write(self, setting_key: str, source: str, value: int) -> None:
        """Set one value. Raises :class:`BackendError` on failure."""


@runtime_checkable
class ClosableBackend(PowerBackend, Protocol):
    def close(self) -> None:  # pragma: no cover - protocol only
        ...


def read_all(backend: PowerBackend) -> Readings:
    """Read every setting QuietGovernor manages, in write order."""
    return {key: backend.read(key) for key in WRITE_ORDER}


def capture_baseline(backend: PowerBackend) -> Baseline:
    """Read the machine's current settings into a :class:`Baseline`."""
    guid, name = backend.active_scheme()
    return Baseline(
        scheme_guid=guid,
        scheme_name=name,
        readings=read_all(backend),
        captured_at=utc_now(),
    )


class SimulatedBackend:
    """An in-memory PC used by tests, ``--simulate`` demos, and docs.

    It behaves like a real machine closely enough to exercise every code
    path: settings can be missing (as they are on many desktops for boost
    mode), and writes can be made to fail partway through so rollback is
    testable.

    When ``state_path`` is given, the simulated machine remembers its own
    values between runs, so a simulated ``apply`` followed by ``status`` and
    ``restore`` behaves exactly like the real thing. That file lives beside
    the real state but is never read in real mode.
    """

    def __init__(
        self,
        values: Mapping[str, Mapping[str, int]] | None = None,
        *,
        scheme_guid: str = "381b4222-f694-41f0-9685-ff5bb260df2e",
        scheme_name: str = "Balanced",
        unavailable: Iterable[str] = (),
        fail_on: tuple[str, str] | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self.scheme_guid = scheme_guid
        self.scheme_name = scheme_name
        self.state_path = Path(state_path) if state_path is not None else None
        self._values: dict[str, dict[str, int]] = {}
        for key in SETTINGS:
            default = {"min_state": 5, "max_state": 100, "boost_mode": 1}[key]
            per_source = {source: default for source in POWER_SOURCES}
            per_source.update(dict((values or {}).get(key, {})))
            self._values[key] = per_source
        self._unavailable = set(unavailable)
        self._fail_on = fail_on
        self.writes: list[tuple[str, str, int]] = []
        self._load()

    def active_scheme(self) -> tuple[str, str]:
        return self.scheme_guid, self.scheme_name

    def read(self, setting_key: str) -> Reading:
        if setting_key not in SETTINGS:
            raise BackendError(f"unknown setting {setting_key!r}")
        if setting_key in self._unavailable:
            return Reading(available=False, values={})
        return Reading(available=True, values=dict(self._values[setting_key]))

    def write(self, setting_key: str, source: str, value: int) -> None:
        if setting_key not in SETTINGS:
            raise BackendError(f"unknown setting {setting_key!r}")
        if source not in (AC, DC):
            raise BackendError(f"unknown power source {source!r}")
        if setting_key in self._unavailable:
            raise BackendError(
                f"This PC does not report {SETTINGS[setting_key].label}, so it "
                "cannot be changed."
            )
        if self._fail_on == (setting_key, source):
            raise BackendError(
                "The simulated power service rejected this change (test mode)."
            )
        self.writes.append((setting_key, source, value))
        self._values[setting_key][source] = value
        self._save()

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            values = payload["values"]
            self.scheme_guid = str(payload.get("scheme_guid", self.scheme_guid))
            self.scheme_name = str(payload.get("scheme_name", self.scheme_name))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            # A damaged simulation file is not worth an error: rebuild it.
            return
        for key, per_source in values.items():
            if key in self._values and isinstance(per_source, dict):
                for source, value in per_source.items():
                    if source in (AC, DC) and isinstance(value, int) and not isinstance(value, bool):
                        self._values[key][source] = value

    def _save(self) -> None:
        if self.state_path is None:
            return
        payload: dict[str, Any] = {
            "scheme_guid": self.scheme_guid,
            "scheme_name": self.scheme_name,
            "values": {key: dict(per_source) for key, per_source in self._values.items()},
            "note": "Simulated PC state. Not used in real mode.",
        }
        atomic_write_text(
            self.state_path,
            json.dumps(payload, indent=2) + "\n",
        )

    def effective_values(self) -> dict[str, dict[str, int]]:
        """The simulated machine's current values, for assertions."""
        return {key: dict(per_source) for key, per_source in self._values.items()}


def drift(
    readings: Readings,
    managed_values: Mapping[str, Mapping[str, int]],
) -> list[tuple[str, str, int | None, int]]:
    """Compare live readings with managed values.

    Returns ``(setting_key, source, current, managed)`` for every managed
    value that the machine no longer reports, which means something other
    than QuietGovernor changed it (a Windows power-plan change, another
    tuning tool, a driver reset).
    """
    out: list[tuple[str, str, int | None, int]] = []
    for key in WRITE_ORDER:
        per_source = managed_values.get(key, {})
        reading = readings.get(key)
        for source in POWER_SOURCES:
            managed = per_source.get(source)
            if managed is None:
                continue
            current = reading.value_for(source) if reading is not None else None
            if current != managed:
                out.append((key, source, current, managed))
    return out
