"""Profiles, power-setting definitions, and validation.

This module is pure data plus pure functions: no I/O, no Windows calls.
It is the single place that knows which Windows power settings
QuietGovernor is allowed to touch, and what values are acceptable for
them. Everything else (planning, controller, CLI) asks this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .errors import PolicyError


# --- Windows power setting identifiers -------------------------------------
#
# These GUIDs come from the documented "Processor power management"
# subgroup (SUB_PROCESSOR). Each pair is (subgroup, setting), matched across
# AC (plugged in) and DC (battery) power sources. They are load-bearing:
# tests/test_policy.py pins them so a typo cannot silently ship.

SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"

#: Minimum processor state, percent (powercfg alias PROCTHROTTLEMIN).
SETTING_MIN_STATE = "893dee8e-2bef-41e0-89c6-b55d0929964c"
#: Maximum processor state, percent (powercfg alias PROCTHROTTLEMAX).
SETTING_MAX_STATE = "bc5038f7-23e0-4960-96da-33abaf5935ec"
#: Processor performance boost mode (powercfg alias PERFBOOSTMODE).
#: 0 disabled, 1 enabled, 2 aggressive, 3 efficient, 4 aggressive at
#: guaranteed, 5 efficient at guaranteed. Hidden by default in Windows.
SETTING_BOOST_MODE = "be337238-0d82-4146-a960-4f3749d470c7"


@dataclass(frozen=True)
class Setting:
    """One Windows power setting QuietGovernor can read and write."""

    key: str
    label: str
    subgroup: str
    guid: str
    minimum: int
    maximum: int
    unit: str
    #: Human descriptions keyed by raw value, used in explanations.
    meanings: Mapping[int, str]

    def describe(self, value: int) -> str:
        """Explain a raw setting value in words."""
        known = self.meanings.get(value)
        if known is not None:
            return known
        if self.unit == "%":
            return f"{value}% of full processor speed"
        return f"boost mode {value}"

    def validate(self, value: object) -> int:
        """Return ``value`` as an int, or raise ValueError with a fix path."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise PolicyError(
                f"{self.label} must be a whole number of {self.unit} "
                f"between {self.minimum} and {self.maximum}, got {value!r}."
            )
        if not self.minimum <= value <= self.maximum:
            raise PolicyError(
                f"{self.label} must be between {self.minimum} and "
                f"{self.maximum}{self.unit}, got {value}{self.unit}. "
                f"Pick a value inside that range."
            )
        return value


def _percent_meanings() -> dict[int, str]:
    return {
        0: "the processor may idle at its lowest power state",
        100: "the processor may reach full clock speed",
    }


_MIN_STATE_MEANINGS = {
    **_percent_meanings(),
    5: "the processor stays at or above 5% of full speed",
    25: "the processor stays at or above a quarter of full speed",
    50: "the processor stays at or above half of full speed",
}
_MAX_STATE_MEANINGS = {
    **_percent_meanings(),
    60: "the processor is capped at 60% of full speed",
    70: "the processor is capped at 70% of full speed",
    80: "the processor is capped at 80% of full speed",
}

SETTINGS: Mapping[str, Setting] = {
    # Raising the minimum keeps clocks high (faster, louder). QuietGovernor
    # caps it at 50 so a profile can never pin the CPU at full speed.
    "min_state": Setting(
        key="min_state",
        label="Minimum processor state",
        subgroup=SUB_PROCESSOR,
        guid=SETTING_MIN_STATE,
        minimum=0,
        maximum=50,
        unit="%",
        meanings=_MIN_STATE_MEANINGS,
    ),
    "max_state": Setting(
        key="max_state",
        label="Maximum processor state",
        subgroup=SUB_PROCESSOR,
        guid=SETTING_MAX_STATE,
        minimum=20,
        maximum=100,
        unit="%",
        meanings=_MAX_STATE_MEANINGS,
    ),
    "boost_mode": Setting(
        key="boost_mode",
        label="Processor performance boost mode",
        subgroup=SUB_PROCESSOR,
        guid=SETTING_BOOST_MODE,
        minimum=0,
        maximum=5,
        unit="",
        meanings={
            0: "boost is off — the processor stays at or below its rated speed",
            1: "boost is on — the processor may exceed its rated speed briefly",
            2: "boost is aggressive — the processor boosts more readily",
            3: "boost is efficient — the processor boosts only when it pays off",
            4: "boost is aggressive at guaranteed speed",
            5: "boost is efficient at guaranteed speed",
        },
    ),
}

#: The order settings are written in. Boost is changed before the state cap
#: so a machine never sits in "max state 30%, boost aggressive" for longer
#: than necessary.
WRITE_ORDER: tuple[str, ...] = ("boost_mode", "max_state", "min_state")

#: Setting keys every profile must define.
REQUIRED_SETTINGS: tuple[str, ...] = ("max_state", "min_state")

#: Exact note appended wherever boost mode is unavailable, so explanations
#: stay consistent between ``plan``, ``apply``, and the documentation.
BOOST_UNAVAILABLE_NOTE = (
    "This PC does not expose a processor performance boost mode setting, so "
    "it was left unchanged. That is normal on many desktops, on virtual "
    "machines, and on CPUs without turbo support."
)

AC = "AC"
DC = "DC"
POWER_SOURCES: tuple[str, ...] = (AC, DC)

DEFAULT_SCOPE = "both"
SCOPES: Mapping[str, tuple[str, ...]] = {
    "both": (AC, DC),
    "ac": (AC,),
    "dc": (DC,),
}

STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Profile:
    """A named, validated set of processor power values.

    A profile is either *resolved* (it has concrete baseline values for every
    setting and power source it governs) or *partial* (it only overrides the
    values a user named, and inherits the rest from the machine's current
    settings). ``quiet``, ``balanced``, and ``custom`` are resolved;
    ``restore`` is the partial profile that removes every override.
    """

    name: str
    description: str
    values: Mapping[str, Mapping[str, int]]
    scope: str = DEFAULT_SCOPE
    partial: bool = False
    origin: str = "built-in"
    #: (setting, source) pairs filled in from the PC's live values because the
    #: profile did not name them. Never persisted; used only by planning.
    inherited: frozenset[tuple[str, str]] = frozenset()

    @property
    def sources(self) -> tuple[str, ...]:
        """Power sources this profile governs, in canonical order."""
        return SCOPES[self.scope]

    def is_inherited(self, setting_key: str, source: str) -> bool:
        return (setting_key, source) in self.inherited

    def value_for(self, setting_key: str, source: str) -> int | None:
        """Return the profile's value, or None when it does not override it."""
        return self.values.get(setting_key, {}).get(source)

    def resolved_values(self) -> list[tuple[str, str, int]]:
        """Every (setting, source, value) triple, in write order."""
        out: list[tuple[str, str, int]] = []
        for key in WRITE_ORDER:
            for source in self.sources:
                value = self.value_for(key, source)
                if value is not None:
                    out.append((key, source, value))
        return out

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "values": {
                key: {source: value for source, value in per_source.items()}
                for key, per_source in self.values.items()
            },
            "scope": self.scope,
            "partial": self.partial,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Profile":
        """Rebuild a profile from stored data, validating every value.

        Raises ValueError when the stored data is malformed or out of range,
        which the store turns into a StoreError naming the file.
        """
        if not isinstance(data, Mapping):
            raise PolicyError("profile must be an object")
        try:
            name = data["name"]
            description = data.get("description", "")
            raw_values = data["values"]
            scope = data.get("scope", DEFAULT_SCOPE)
            partial = data.get("partial", False)
            origin = data.get("origin", "custom")
        except KeyError as missing:
            raise PolicyError(f"profile is missing the {missing.args[0]!r} field") from None
        if not isinstance(name, str) or not name.strip():
            raise PolicyError("profile name must be a non-empty string")
        validate_scope(scope if isinstance(scope, str) else "")
        if not isinstance(raw_values, Mapping):
            raise PolicyError("profile 'values' must be an object")
        values: dict[str, dict[str, int]] = {}
        for key, per_source in raw_values.items():
            if key not in SETTINGS:
                raise PolicyError(f"profile names unknown setting {key!r}")
            if not isinstance(per_source, Mapping):
                raise PolicyError(f"profile setting {key!r} must map power sources to values")
            validated: dict[str, int] = {}
            for source, value in per_source.items():
                if source not in POWER_SOURCES:
                    raise PolicyError(
                        f"profile setting {key!r} has unknown power source {source!r}; "
                        f"expected {' or '.join(POWER_SOURCES)}"
                    )
                validated[source] = SETTINGS[key].validate(value)
            values[key] = validated
        return cls(
            name=name,
            description=str(description),
            values=values,
            scope=scope,
            partial=bool(partial),
            origin=str(origin),
        )


def validate_scope(scope: str) -> str:
    """Return ``scope`` if it names a supported power-source scope."""
    if scope not in SCOPES:
        raise PolicyError(
            f"scope must be one of {', '.join(SCOPES)}, got {scope!r}. "
            "Use 'both' to govern battery and plugged-in power together."
        )
    return scope


def validate_value(setting_key: str, value: object) -> int:
    """Validate a single setting value, raising ValueError with a fix path."""
    try:
        setting = SETTINGS[setting_key]
    except KeyError:
        raise PolicyError(
            f"unknown setting {setting_key!r}; choose from {', '.join(SETTINGS)}."
        ) from None
    return setting.validate(value)


def check_consistency(values: Mapping[str, Mapping[str, int]]) -> None:
    """Reject a set of overrides whose minimum exceeds its maximum.

    ``values`` maps setting keys to per-source values. A setting that is
    missing on a source is ignored, because it will be inherited from the
    machine rather than written.
    """
    for source in POWER_SOURCES:
        low = values.get("min_state", {}).get(source)
        high = values.get("max_state", {}).get(source)
        if low is not None and high is not None and low > high:
            raise PolicyError(
                f"minimum processor state ({low}%) is higher than maximum "
                f"processor state ({high}%) for {_source_label(source)} power. "
                "Lower the minimum or raise the maximum so the minimum is not "
                "above the maximum."
            )


def _source_label(source: str) -> str:
    return "plugged-in" if source == AC else "battery"


def build_profile(
    name: str,
    description: str,
    values: Mapping[str, Mapping[str, int]],
    *,
    scope: str = DEFAULT_SCOPE,
    partial: bool = False,
) -> Profile:
    """Validate raw values and return a :class:`Profile`."""
    validate_scope(scope)
    validated: dict[str, dict[str, int]] = {}
    for key, per_source in values.items():
        validate_value_key(key)
        if not isinstance(per_source, Mapping):
            raise PolicyError(f"values for {key!r} must map power sources to numbers")
        per_source_validated: dict[str, int] = {}
        for source, value in per_source.items():
            if source not in SCOPES[scope]:
                raise PolicyError(
                    f"setting {key!r} gives a value for {source!r} power, but "
                    f"scope {scope!r} only covers {', '.join(SCOPES[scope])}. "
                    f"Drop that value or use scope 'both'."
                )
            per_source_validated[source] = validate_value(key, value)
        validated[key] = per_source_validated
    check_consistency(validated)
    return Profile(
        name=name,
        description=description,
        values=validated,
        scope=scope,
        partial=partial,
    )


def validate_value_key(key: str) -> str:
    if key not in SETTINGS:
        raise PolicyError(
            f"unknown setting {key!r}; choose from {', '.join(SETTINGS)}."
        )
    return key


def _two_source(max_state: int, min_state: int, boost: int) -> dict[str, dict[str, int]]:
    return {
        "boost_mode": {AC: boost, DC: boost},
        "max_state": {AC: max_state, DC: max_state},
        "min_state": {AC: min_state, DC: min_state},
    }


def _builtin_profiles() -> dict[str, Profile]:
    quiet = build_profile(
        "quiet",
        "Cap the processor below full speed and turn off boost so fans stay "
        "quiet. Best for writing, browsing, and calls on a fan-noisy laptop.",
        _two_source(max_state=60, min_state=0, boost=0),
    )
    balanced = build_profile(
        "balanced",
        "A gentler cap that still lets the processor boost when it needs to. "
        "A reasonable everyday default.",
        _two_source(max_state=85, min_state=0, boost=1),
    )
    custom = build_profile(
        "custom",
        "Starting point for your own values. Nothing changes until you edit it.",
        _two_source(max_state=100, min_state=0, boost=1),
    )
    restore = Profile(
        name="restore",
        description=(
            "Put every setting QuietGovernor manages back the way it was "
            "before it was first applied."
        ),
        values={},
        partial=True,
        origin="built-in",
    )
    return {"quiet": quiet, "balanced": balanced, "custom": custom, "restore": restore}


#: Profiles that ship with QuietGovernor, keyed by name.
BUILTIN_PROFILES: Mapping[str, Profile] = _builtin_profiles()


def get_builtin(name: str) -> Profile:
    try:
        return BUILTIN_PROFILES[name]
    except KeyError:
        raise PolicyError(
            f"there is no built-in profile called {name!r}; "
            f"choose from {', '.join(sorted(BUILTIN_PROFILES))}."
        ) from None


_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def is_guid(text: str) -> bool:
    """True when ``text`` looks like a canonical Windows GUID."""
    return bool(_GUID_RE.match(text))


def iter_settings() -> Iterable[Setting]:
    """Settings in write order."""
    for key in WRITE_ORDER:
        yield SETTINGS[key]
