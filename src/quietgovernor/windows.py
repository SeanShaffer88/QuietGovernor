"""Windows power-plan access through ``powrprof.dll``.

Only this module knows about Windows. It binds the documented Power
Management API:

    PowerGetActiveScheme      -- which plan is active
    PowerReadACValueIndex     -- current value on mains power
    PowerReadDCValueIndex     -- current value on battery
    PowerWriteACValueIndex    -- set the value on mains power
    PowerWriteDCValueIndex    -- set the value on battery
    PowerReadFriendlyName     -- the plan's display name

The C API is used instead of ``powercfg.exe`` because ``powercfg`` output is
localised and its text differs between Windows versions; parsing it would
break on non-English PCs and on future releases. Errors are reported as the
Win32 code plus the system's own message.

Verification status: this module has **not been run on Windows hardware** by
the project. It compiles and imports everywhere, and GUID packing is unit
tested against ``uuid``, but every call here is labelled Unverified in
docs/SPEC.md until a real run is recorded.
"""

from __future__ import annotations

import ctypes
import sys
import uuid
from ctypes import wintypes

from .errors import BackendError, NotElevatedError
from .planning import Reading
from .policy import AC, DC, SETTINGS

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_PARAMETER = 87
ERROR_NOT_FOUND = 1168

#: Error codes that mean "this PC does not have this setting", as opposed to
#: "the call failed". Windows returns different ones across versions.
_MISSING_CODES = {ERROR_FILE_NOT_FOUND, ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND}

ELEVATION_HELP = (
    "Changing power settings needs administrator rights. Windows refused with "
    "'access denied'. Right-click your terminal and choose 'Run as "
    "administrator' (or start an elevated Command Prompt), then run the same "
    "command again. Nothing was changed by this attempt."
)

NOT_WINDOWS_HELP = (
    "QuietGovernor's real mode needs Windows: it changes processor power "
    "settings through the Windows power-management API. This computer is "
    "running {platform}. On a non-Windows machine you can still use "
    "'--simulate' to explore profiles, plans, storage, and restore behaviour; "
    "nothing in simulation mode touches the operating system."
)


class GUID(ctypes.Structure):
    """The Win32 ``GUID`` layout: exactly 16 bytes, no padding.

    Fixed-width ctypes types are used on purpose. ``wintypes.DWORD`` is an
    alias for C ``unsigned long``, which is 8 bytes off Windows, and the
    default struct alignment would grow the record; either mistake would make
    every call write to the wrong place, so the size is asserted below.
    """

    _pack_ = 1
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, text: str) -> "GUID":
        """Build a GUID from canonical text, matching Win32 byte order."""
        try:
            raw = uuid.UUID(text).bytes_le
        except ValueError as exc:
            raise BackendError(
                f"{text!r} is not a valid Windows GUID, so the setting it names "
                "cannot be read or written. This is a QuietGovernor bug; please "
                "report it with the command you ran."
            ) from exc
        if len(raw) != 16:  # pragma: no cover - uuid always yields 16
            raise BackendError(f"{text!r} did not convert to 16 bytes.")
        return cls.from_buffer_copy(raw)

    def to_string(self) -> str:
        return str(uuid.UUID(bytes_le=bytes(self)))


assert ctypes.sizeof(GUID) == 16, "GUID must be 16 bytes"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_elevated() -> bool:
    """True when this process can write system power settings."""
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # pragma: no cover - exotic hosts
        return False


def _error_text(code: int) -> str:
    try:
        text = ctypes.FormatError(code)  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # pragma: no cover - non-Windows
        text = ""
    text = (text or "").strip()
    return f"Windows error {code}{': ' + text if text else ''}"


def _load_powrprof() -> "ctypes.CDLL":
    if not is_windows():
        import platform

        raise BackendError(NOT_WINDOWS_HELP.format(platform=platform.system()))
    try:
        library = ctypes.WinDLL("powrprof.dll")  # type: ignore[attr-defined]
    except OSError as exc:  # pragma: no cover - only on broken Windows
        raise BackendError(
            f"Could not load powrprof.dll ({exc}). QuietGovernor changes "
            "processor power settings through that Windows component, so "
            "without it nothing can be applied. On a repaired or minimal "
            "Windows install, run 'sfc /scannow' as administrator."
        ) from exc

    library.PowerGetActiveScheme.argtypes = [
        wintypes.HKEY,
        ctypes.POINTER(ctypes.POINTER(GUID)),
    ]
    library.PowerGetActiveScheme.restype = ctypes.c_uint32
    for name in ("PowerReadACValueIndex", "PowerReadDCValueIndex"):
        function = getattr(library, name)
        function.argtypes = [
            wintypes.HKEY,
            ctypes.POINTER(GUID),
            ctypes.POINTER(GUID),
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        function.restype = ctypes.c_uint32
    for name in ("PowerWriteACValueIndex", "PowerWriteDCValueIndex"):
        function = getattr(library, name)
        function.argtypes = [
            wintypes.HKEY,
            ctypes.POINTER(GUID),
            ctypes.POINTER(GUID),
            ctypes.POINTER(GUID),
            wintypes.DWORD,
        ]
        function.restype = ctypes.c_uint32
    library.PowerReadFriendlyName.argtypes = [
        wintypes.HKEY,
        ctypes.POINTER(GUID),
        ctypes.POINTER(GUID),
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    library.PowerReadFriendlyName.restype = ctypes.c_uint32
    return library


class WindowsBackend:
    """The real machine's active power plan."""

    def __init__(self) -> None:
        self._library = _load_powrprof()
        self._kernel32 = ctypes.WinDLL("kernel32.dll")  # type: ignore[attr-defined]
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL
        self._scheme = self._read_scheme()
        self._cache: dict[str, GUID] = {}

    # -- helpers -----------------------------------------------------------

    def _guid(self, text: str) -> GUID:
        cached = self._cache.get(text)
        if cached is None:
            cached = GUID.from_string(text)
            self._cache[text] = cached
        return cached

    def _scheme_pointer(self) -> "ctypes.POINTER(GUID)":
        return ctypes.pointer(self._scheme)

    def _read_scheme(self) -> GUID:
        pointer = ctypes.POINTER(GUID)()
        code = self._library.PowerGetActiveScheme(None, ctypes.byref(pointer))
        if code != ERROR_SUCCESS:
            raise BackendError(
                f"Could not read the active power plan ({_error_text(code)}). "
                "QuietGovernor needs to know which plan it is changing before "
                "it changes anything. Try opening Power Options in Windows "
                "once, then run the command again."
            )
        try:
            return GUID.from_buffer_copy(pointer.contents)
        finally:
            self._kernel32.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))

    def _friendly_name(self) -> str:
        size = ctypes.c_uint32(0)
        code = self._library.PowerReadFriendlyName(
            None, self._scheme_pointer(), None, None, None, ctypes.byref(size)
        )
        if code != ERROR_SUCCESS or size.value == 0:
            return "the active power plan"
        buffer = (ctypes.c_ubyte * size.value)()
        code = self._library.PowerReadFriendlyName(
            None,
            self._scheme_pointer(),
            None,
            None,
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.byref(size),
        )
        if code != ERROR_SUCCESS:
            return "the active power plan"
        return bytes(buffer).decode("utf-16-le", errors="replace").rstrip("\x00")

    def _read_index(self, source: str, setting_key: str) -> int | None:
        setting = SETTINGS[setting_key]
        subgroup = self._guid(setting.subgroup)
        target = self._guid(setting.guid)
        value = ctypes.c_uint32(0)
        reader = (
            self._library.PowerReadACValueIndex
            if source == AC
            else self._library.PowerReadDCValueIndex
        )
        code = reader(
            None,
            self._scheme_pointer(),
            ctypes.byref(subgroup),
            ctypes.byref(target),
            ctypes.byref(value),
        )
        if code == ERROR_SUCCESS:
            return int(value.value)
        if code in _MISSING_CODES:
            return None
        if code == ERROR_ACCESS_DENIED:
            raise NotElevatedError(ELEVATION_HELP)
        raise BackendError(
            f"Could not read {setting.label} for "
            f"{'plugged-in' if source == AC else 'battery'} power "
            f"({_error_text(code)}). Nothing was changed. If this repeats, "
            "check that Windows' own Power Options page opens normally."
        )

    # -- PowerBackend protocol --------------------------------------------

    def active_scheme(self) -> tuple[str, str]:
        return self._scheme.to_string(), self._friendly_name()

    def read(self, setting_key: str) -> Reading:
        if setting_key not in SETTINGS:
            raise BackendError(f"QuietGovernor does not manage {setting_key!r}.")
        ac = self._read_index(AC, setting_key)
        dc = self._read_index(DC, setting_key)
        if ac is None and dc is None:
            return Reading(available=False, values={})
        return Reading(available=True, values={AC: ac, DC: dc})

    def ensure_writable(self) -> None:
        """Fail early with an actionable message instead of mid-rollback."""
        if not is_elevated():
            raise NotElevatedError(ELEVATION_HELP)

    def write(self, setting_key: str, source: str, value: int) -> None:
        setting = SETTINGS[setting_key]
        subgroup = self._guid(setting.subgroup)
        target = self._guid(setting.guid)
        writer = (
            self._library.PowerWriteACValueIndex
            if source == AC
            else self._library.PowerWriteDCValueIndex
        )
        code = writer(
            None,
            self._scheme_pointer(),
            ctypes.byref(subgroup),
            ctypes.byref(target),
            ctypes.c_uint32(value),
        )
        if code == ERROR_SUCCESS:
            return
        if code == ERROR_ACCESS_DENIED:
            raise NotElevatedError(ELEVATION_HELP)
        raise BackendError(
            f"Windows refused to set {setting.label} to {value} for "
            f"{'plugged-in' if source == AC else 'battery'} power "
            f"({_error_text(code)})."
        )
