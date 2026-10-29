"""Versioned, atomically written state files.

Three files live in one directory (``%LOCALAPPDATA%\\QuietGovernor`` on
Windows, overridable with ``--state-dir`` or ``QUIETGOVERNOR_STATE_DIR``):

* ``baseline.json`` — the power settings captured before the first change.
  Written **once** and never overwritten, because it is the evidence needed
  to undo everything.
* ``state.json`` — what QuietGovernor currently manages.
* ``profiles.json`` — user-defined profiles.

Every file is enveloped as ``{"kind": ..., "schema_version": 1, "data": ...}``
so a wrong file, a truncated file, or a future format is reported clearly
instead of being half-understood. Writes are atomic: a crash leaves either
the old file or the new one, never a partial one.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .errors import StoreError
from .planning import Baseline, utc_now
from .policy import POWER_SOURCES, SETTINGS, validate_value

SCHEMA_VERSION = 1
BASELINE_FILE = "baseline.json"
STATE_FILE = "state.json"
PROFILES_FILE = "profiles.json"

STATE_DIR_ENV = "QUIETGOVERNOR_STATE_DIR"


def default_state_dir() -> Path:
    """Where state lives by default: per-user, no elevation needed."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        return Path(appdata) / "QuietGovernor"
    return Path.home() / ".quietgovernor"


def resolved_state_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    return Path(explicit).expanduser() if explicit else default_state_dir()


# --- low-level file helpers ------------------------------------------------


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` so readers never see a partial file.

    The temporary file is created in the destination directory (so the final
    ``os.replace`` is atomic on the same volume), flushed to disk, then moved
    into place. Permissions are restricted because these files describe the
    machine's configuration.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_path, mode)
        except OSError:  # pragma: no cover - unusual filesystems
            pass
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _fsync_dir(directory: Path) -> None:
    """Flush the directory entry so a rename survives a power cut."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - Windows and some filesystems
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _dump(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def write_document(path: Path, kind: str, data: Mapping[str, Any]) -> None:
    payload = {"kind": kind, "schema_version": SCHEMA_VERSION, "data": data}
    text = _dump(payload)
    atomic_write_text(path, text)
    # Read back what we wrote: a disk that silently truncates would otherwise
    # turn into an unrestorable machine later.
    try:
        reread = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(
            f"QuietGovernor wrote {path} but could not read it back "
            f"({exc}). Your previous settings are still recorded elsewhere in "
            "that folder; check that the disk is not full or read-only."
        ) from exc
    if reread != payload:
        raise StoreError(
            f"QuietGovernor wrote {path} but the file read back differently. "
            "The disk may be failing or full. Your machine's power settings "
            "were not changed by that write."
        )


def read_document(path: Path, kind: str) -> dict[str, Any] | None:
    """Read and validate the envelope. Returns None when the file is absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError(
            f"Could not read {path} ({exc}). Check that the file is readable "
            "and not locked by another program."
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StoreError(
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}). "
            "It was probably edited by hand or truncated. Move or delete it and "
            "run the command again; QuietGovernor rebuilds it from the PC."
        ) from exc
    if not isinstance(payload, dict):
        raise StoreError(f"{path} does not contain a JSON object. Move it aside and retry.")
    if payload.get("kind") != kind:
        raise StoreError(
            f"{path} is a {payload.get('kind')!r} file, but a {kind!r} file was "
            "expected. Move it aside; QuietGovernor will create the right one."
        )
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StoreError(
            f"{path} uses schema version {version!r}, but this build of "
            f"QuietGovernor understands version {SCHEMA_VERSION}. Upgrade "
            "QuietGovernor, or move the file aside to start fresh (your PC's "
            "settings are not affected by deleting it)."
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise StoreError(f"{path} has no usable data section. Move it aside and retry.")
    return data


# --- typed documents -------------------------------------------------------


@dataclass(frozen=True)
class AppliedState:
    """What QuietGovernor is currently managing on this PC.

    ``managed_values`` is the intent, not a claim about the machine: the
    machine is always checked live, so an outside change (or the Windows
    user changing the plan by hand) is visible as drift rather than being
    silently overwritten in a report.
    """

    profile_name: str
    managed_values: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    applied_at: str = field(default_factory=utc_now)
    scheme_guid: str = ""
    scheme_name: str = ""
    profile_origin: str = "built-in"

    @property
    def is_governing(self) -> bool:
        return any(self.managed_values.values())

    def managed_value(self, setting_key: str, source: str) -> int | None:
        return self.managed_values.get(setting_key, {}).get(source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "profile_origin": self.profile_origin,
            "managed_values": {
                key: {source: value for source, value in per_source.items()}
                for key, per_source in self.managed_values.items()
            },
            "applied_at": self.applied_at,
            "scheme_guid": self.scheme_guid,
            "scheme_name": self.scheme_name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AppliedState":
        profile_name = data.get("profile_name")
        if not isinstance(profile_name, str) or not profile_name:
            raise ValueError("state is missing the profile name")
        raw = data.get("managed_values", {})
        if not isinstance(raw, Mapping):
            raise ValueError("state 'managed_values' must be an object")
        managed: dict[str, dict[str, int]] = {}
        for key, per_source in raw.items():
            if key not in SETTINGS:
                raise ValueError(f"state names unknown setting {key!r}")
            if not isinstance(per_source, Mapping):
                raise ValueError(f"state setting {key!r} must map power sources to values")
            managed[key] = {}
            for source, value in per_source.items():
                if source not in POWER_SOURCES:
                    raise ValueError(f"state setting {key!r} has unknown power source {source!r}")
                managed[key][source] = validate_value(key, value)
        return cls(
            profile_name=profile_name,
            managed_values=managed,
            applied_at=str(data.get("applied_at", "")),
            scheme_guid=str(data.get("scheme_guid", "")),
            scheme_name=str(data.get("scheme_name", "")),
            profile_origin=str(data.get("profile_origin", "built-in")),
        )


class StateStore:
    """Reads and writes the three state files in one directory."""

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self.directory = resolved_state_dir(directory)

    @property
    def baseline_path(self) -> Path:
        return self.directory / BASELINE_FILE

    @property
    def state_path(self) -> Path:
        return self.directory / STATE_FILE

    @property
    def profiles_path(self) -> Path:
        return self.directory / PROFILES_FILE

    # baseline --------------------------------------------------------------

    def load_baseline(self) -> Baseline | None:
        data = read_document(self.baseline_path, "baseline")
        if data is None:
            return None
        try:
            return Baseline.from_dict(data)
        except ValueError as exc:
            raise StoreError(
                f"{self.baseline_path} could not be understood: {exc}. This file "
                "is your only record of the original power settings; copy it "
                "somewhere safe before deleting it, then re-apply a profile to "
                "start a new record."
            ) from exc

    def ensure_baseline(self, baseline: Baseline) -> Baseline:
        """Return the stored baseline, saving ``baseline`` only if none exists.

        The first capture wins. Overwriting it with currently-applied values
        would make restore a no-op, which is the classic way this kind of tool
        traps users in a changed state.
        """
        existing = self.load_baseline()
        if existing is not None:
            return existing
        self.save_baseline(baseline)
        return baseline

    def save_baseline(self, baseline: Baseline) -> None:
        write_document(self.baseline_path, "baseline", baseline.to_dict())

    # state -----------------------------------------------------------------

    def load_state(self) -> AppliedState | None:
        data = read_document(self.state_path, "state")
        if data is None:
            return None
        try:
            return AppliedState.from_dict(data)
        except ValueError as exc:
            raise StoreError(
                f"{self.state_path} could not be understood: {exc}. Deleting "
                "this file only forgets what QuietGovernor had applied; your "
                "restore point in baseline.json is kept, so 'quietgovernor "
                "restore' will still work."
            ) from exc

    def save_state(self, state: AppliedState) -> None:
        write_document(self.state_path, "state", state.to_dict())

    def clear_state(self) -> None:
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StoreError(f"Could not remove {self.state_path} ({exc}).") from exc

    # user profiles ---------------------------------------------------------

    def load_user_profiles(self) -> dict[str, Any]:
        """Raw stored profile objects, keyed by name."""
        data = read_document(self.profiles_path, "profiles")
        if data is None:
            return {}
        raw = data.get("profiles", {})
        if not isinstance(raw, Mapping):
            raise StoreError(
                f"{self.profiles_path} has a 'profiles' section that is not an "
                "object. Fix or delete the file; built-in profiles always work."
            )
        return {str(name): dict(value) for name, value in raw.items() if isinstance(value, Mapping)}

    def save_user_profiles(self, profiles: Mapping[str, Mapping[str, Any]]) -> None:
        write_document(
            self.profiles_path,
            "profiles",
            {"profiles": {name: dict(body) for name, body in profiles.items()}},
        )
