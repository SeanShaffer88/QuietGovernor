"""Baselines, readings, and write plans.

A *reading* is what the machine currently has for one setting.
A *baseline* is the full set of readings captured the first time
QuietGovernor changed anything, so it can put them back exactly.
A *plan* is the reviewable difference between a profile and reality.

Planning never writes. ``apply`` shows the plan, asks for confirmation, and
only then executes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .errors import BackendError
from .policy import (
    AC,
    BOOST_UNAVAILABLE_NOTE,
    DC,
    SETTINGS,
    WRITE_ORDER,
    Profile,
)


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string without microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _source_label(source: str) -> str:
    return "plugged-in" if source == AC else "battery"


@dataclass(frozen=True)
class Reading:
    """What the machine currently reports for one setting."""

    available: bool
    values: Mapping[str, int | None]

    def value_for(self, source: str) -> int | None:
        if not self.available:
            return None
        return self.values.get(source)

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "values": {source: value for source, value in self.values.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Reading":
        if not isinstance(data, Mapping):
            raise ValueError("reading must be an object")
        available = data.get("available", False)
        raw = data.get("values", {})
        if not isinstance(available, bool):
            raise ValueError("reading 'available' must be true or false")
        if not isinstance(raw, Mapping):
            raise ValueError("reading 'values' must be an object")
        values: dict[str, int | None] = {}
        for source, value in raw.items():
            if source not in (AC, DC):
                raise ValueError(f"reading has unknown power source {source!r}")
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise ValueError(
                    f"reading for {source!r} must be a whole number or null"
                )
            values[source] = value
        return cls(available=available, values=values)


Readings = Mapping[str, Reading]


@dataclass(frozen=True)
class Baseline:
    """The power settings as they were before QuietGovernor changed anything."""

    scheme_guid: str
    scheme_name: str
    readings: Readings
    captured_at: str
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "scheme_guid": self.scheme_guid,
            "scheme_name": self.scheme_name,
            "readings": {key: reading.to_dict() for key, reading in self.readings.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Baseline":
        if not isinstance(data, Mapping):
            raise ValueError("baseline must be an object")
        version = data.get("schema_version")
        if version != 1:
            raise ValueError(
                f"unsupported baseline schema version {version!r}; "
                "this build understands version 1"
            )
        scheme_guid = data.get("scheme_guid")
        if not isinstance(scheme_guid, str) or not scheme_guid:
            raise ValueError("baseline is missing the power plan identifier")
        raw_readings = data.get("readings")
        if not isinstance(raw_readings, Mapping):
            raise ValueError("baseline is missing its readings")
        readings: dict[str, Reading] = {}
        for key, raw in raw_readings.items():
            if key not in SETTINGS:
                raise ValueError(f"baseline names unknown setting {key!r}")
            readings[key] = Reading.from_dict(raw)  # type: ignore[arg-type]
        captured_at = data.get("captured_at", "")
        scheme_name = data.get("scheme_name", "")
        return cls(
            scheme_guid=scheme_guid,
            scheme_name=str(scheme_name),
            readings=readings,
            captured_at=str(captured_at),
        )


@dataclass(frozen=True)
class Change:
    """One setting on one power source moving from ``current`` to ``target``."""

    setting_key: str
    source: str
    current: int | None
    target: int

    @property
    def setting_label(self) -> str:
        return SETTINGS[self.setting_key].label

    def describe(self) -> str:
        """A sentence a non-expert can check against their PC."""
        setting = SETTINGS[self.setting_key]
        was = "unknown" if self.current is None else f"{self.current}{setting.unit}"
        now = f"{self.target}{setting.unit}"
        if self.current is None:
            movement = f"changed from {was} to {now}"
        elif self.target < self.current:
            movement = f"lowered from {was} to {now}"
        else:
            movement = f"raised from {was} to {now}"
        return (
            f"{self.setting_label} on {_source_label(self.source)} power is "
            f"{movement}: {setting.describe(self.target)}."
        )


@dataclass(frozen=True)
class Check:
    """A predicted compatibility check with the PC's own limits."""

    setting_key: str
    source: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Plan:
    """Everything ``apply`` would do, in order, with explanations."""

    profile: Profile
    scheme_guid: str
    scheme_name: str
    changes: tuple[Change, ...]
    unchanged: tuple[Change, ...]
    checks: tuple[Check, ...]
    notes: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def summary(self) -> str:
        if not self.changes:
            return (
                f"Nothing to change: the {self.profile.name} profile already "
                f"matches this PC's current power settings."
            )
        count = len(self.changes)
        noun = "setting" if count == 1 else "settings"
        return (
            f"The {self.profile.name} profile would change {count} {noun} in "
            f"power plan '{self.scheme_name}'."
        )

    def detail_lines(self) -> list[str]:
        lines = [self.summary(), ""]
        for change in self.changes:
            lines.append(f"  * {change.describe()}")
        if self.unchanged:
            lines.append("")
            lines.append("Already at the requested value:")
            for unchanged in self.unchanged:
                lines.append(
                    f"  - {unchanged.setting_label} on "
                    f"{_source_label(unchanged.source)} power "
                    f"({unchanged.target}{SETTINGS[unchanged.setting_key].unit})"
                )
        warnings = [check for check in self.checks if not check.passed]
        if warnings:
            lines.append("")
            lines.append("Things to know before you decide:")
            for check in warnings:
                lines.append(f"  ! {check.detail}")
        if self.notes:
            lines.append("")
            for note in self.notes:
                lines.append(f"Note: {note}")
        return lines


def build_plan(profile: Profile, readings: Readings) -> Plan:
    """Compare ``profile`` with ``readings`` and describe the work.

    Raises :class:`BackendError` when the PC cannot report a required
    setting, because applying a profile without reading the current value
    would make it impossible to restore safely.
    """
    changes: list[Change] = []
    unchanged: list[Change] = []
    notes: list[str] = []
    checks: list[Check] = []

    for key in WRITE_ORDER:
        setting = SETTINGS[key]
        for source in profile.sources:
            target = profile.value_for(key, source)
            if target is None:
                continue
            reading = readings.get(key)
            current = reading.value_for(source) if reading is not None else None
            if current is None:
                if key == "boost_mode":
                    notes.append(BOOST_UNAVAILABLE_NOTE)
                    continue
                raise BackendError(
                    f"This PC did not report {setting.label} for "
                    f"{_source_label(source)} power, so QuietGovernor cannot "
                    "change it safely. No settings were changed. This usually "
                    "means the power plans on this PC have been customised or "
                    "the build is a virtual machine."
                )
            change = Change(key, source, current, target)
            if current == target:
                if not profile.is_inherited(key, source):
                    unchanged.append(change)
            else:
                changes.append(change)
            if key == "max_state":
                checks.append(_max_state_check(target, source))

    if any(
        profile.value_for("max_state", source) not in (None, 100)
        for source in profile.sources
    ):
        notes.append(TURBO_NOTE)

    notes = list(dict.fromkeys(notes))
    return Plan(
        profile=profile,
        scheme_guid="",
        scheme_name="",
        changes=tuple(changes),
        unchanged=tuple(unchanged),
        checks=tuple(checks),
        notes=tuple(notes),
    )


#: Below this maximum, the cap approaches the lowest states Windows exposes.
#: Many processors lose their turbo states entirely in this range and some
#: behave erratically (audio dropouts, stutter) rather than simply slowly, so
#: plans that go this low carry a warning instead of a silent change.
LOW_CAP_WARNING_THRESHOLD = 30


def _max_state_check(target: int, source: str) -> Check:
    """Warn when a maximum-state cap is low enough to be risky."""
    if target >= LOW_CAP_WARNING_THRESHOLD:
        return Check(
            "max_state",
            source,
            True,
            f"A {target}% maximum on {_source_label(source)} power is within "
            "the range Windows handles well.",
        )
    return Check(
        "max_state",
        source,
        False,
        f"Capping {_source_label(source)} power at {target}% is very close to "
        "the lowest states Windows allows. Some processors become unstable or "
        "stutter at this level because their turbo states disappear entirely. "
        f"Consider {LOW_CAP_WARNING_THRESHOLD + 10}% or higher.",
    )


TURBO_NOTE = (
    "Any maximum below 100% turns off turbo boost on most Intel and AMD "
    "processors. That is what makes the PC quieter, and it also lowers peak "
    "speed for short bursts such as builds, games, and video calls."
)


def plan_for_writes(
    profile: Profile,
    scheme_guid: str,
    scheme_name: str,
    readings: Readings,
    extra_notes: Iterable[str] = (),
) -> Plan:
    """Build a plan and attach scheme details plus any caller notes."""
    base = build_plan(profile, readings)
    notes = tuple(dict.fromkeys((*base.notes, *extra_notes)))
    return Plan(
        profile=base.profile,
        scheme_guid=scheme_guid,
        scheme_name=scheme_name,
        changes=base.changes,
        unchanged=base.unchanged,
        checks=base.checks,
        notes=notes,
    )
