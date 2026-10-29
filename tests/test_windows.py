"""Windows bindings.

Most of this file checks the parts that can be verified anywhere: GUID
packing against ``uuid`` (a byte-order mistake here would silently write to
the wrong setting), and the non-Windows failure path. Tests marked
``windows_only`` run only on Windows and are the ones that would produce the
first real evidence for docs/SPEC.md's verification table.
"""

from __future__ import annotations

import sys
import unittest
import uuid

from tests import TempDirTestCase  # noqa: F401

from quietgovernor.errors import BackendError
from quietgovernor.policy import SETTINGS
from quietgovernor.windows import GUID, WindowsBackend, is_elevated, is_windows

IS_WINDOWS = sys.platform.startswith("win")


class TestGuidPacking(unittest.TestCase):
    def test_fields_follow_win32_byte_order(self) -> None:
        guid = GUID.from_string("54533251-82be-4824-96c1-47b60b740d00")
        self.assertEqual(guid.Data1, 0x54533251)
        self.assertEqual(guid.Data2, 0x82BE)
        self.assertEqual(guid.Data3, 0x4824)
        self.assertEqual(bytes(guid.Data4), bytes.fromhex("96c147b60b740d00"))

    def test_exactly_sixteen_bytes(self) -> None:
        self.assertEqual(len(bytes(GUID.from_string(SETTINGS["max_state"].guid))), 16)

    def test_round_trip_through_uuid(self) -> None:
        for text in (
            "893dee8e-2bef-41e0-89c6-b55d0929964c",
            "bc5038f7-23e0-4960-96da-33abaf5935ec",
            "be337238-0d82-4146-a960-4f3749d470c7",
            "00000000-0000-0000-0000-000000000000",
            "ffffffff-ffff-ffff-ffff-ffffffffffff",
        ):
            with self.subTest(text=text):
                guid = GUID.from_string(text)
                self.assertEqual(guid.to_string(), str(uuid.UUID(text)))

    def test_rejects_non_guid_text(self) -> None:
        for text in ("PROCTHROTTLEMAX", "", "12345", "zzzzzzzz-0000-0000-0000-000000000000"):
            with self.subTest(text=text), self.assertRaises(BackendError):
                GUID.from_string(text)

    def test_error_message_points_at_the_guid(self) -> None:
        with self.assertRaises(BackendError) as caught:
            GUID.from_string("not-a-guid")
        self.assertIn("not a valid Windows GUID", str(caught.exception))


class TestPlatformDetection(unittest.TestCase):
    def test_is_windows_matches_the_running_platform(self) -> None:
        self.assertEqual(is_windows(), sys.platform.startswith("win"))

    @unittest.skipIf(IS_WINDOWS, "non-Windows only")
    def test_non_windows_backend_explains_itself(self) -> None:
        with self.assertRaises(BackendError) as caught:
            WindowsBackend()
        message = str(caught.exception)
        self.assertIn("needs Windows", message)
        self.assertIn("--simulate", message)

    @unittest.skipIf(IS_WINDOWS, "non-Windows only")
    def test_elevation_is_false_off_windows(self) -> None:
        self.assertFalse(is_elevated())


@unittest.skipUnless(IS_WINDOWS, "requires Windows")
class TestOnWindows(unittest.TestCase):
    """Read-only checks; these never change a setting."""

    def test_active_scheme_is_a_guid_with_a_name(self) -> None:
        backend = WindowsBackend()
        guid, name = backend.active_scheme()
        self.assertEqual(GUID.from_string(guid).to_string(), guid)
        self.assertTrue(name)

    def test_maximum_processor_state_is_readable(self) -> None:
        backend = WindowsBackend()
        reading = backend.read("max_state")
        self.assertTrue(reading.available, "every PC reports maximum processor state")
        for source in ("AC", "DC"):
            value = reading.value_for(source)
            self.assertTrue(value is None or 0 <= value <= 100)

    def test_unknown_setting_is_refused(self) -> None:
        with self.assertRaises(BackendError):
            WindowsBackend().read("fan_speed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
