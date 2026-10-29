"""QuietGovernor — keep a Windows PC quiet by capping processor performance states.

The package is deliberately split so that everything except the final
Windows API calls can be exercised on any operating system:

    policy    -- what a profile is, and which values are acceptable
    planning  -- turning a profile plus current readings into a write plan
    store     -- versioned, atomically written state files
    backend   -- the power-plan boundary (real Windows, or a simulator)
    controller-- apply / restore with baseline capture and rollback
    windows   -- ctypes bindings to powrprof.dll (Windows only)
    cli       -- the ``quietgovernor`` command
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
