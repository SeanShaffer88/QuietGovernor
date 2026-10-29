"""Apply, restore, and report — the behaviour between the CLI and the backend.

Guarantees this module is responsible for:

1. A baseline is captured **before** the first write, and only once.
2. Nothing is written until the plan has been shown and confirmed.
3. Every write is verified by reading the value back.
4. If a write or verification fails, everything written so far is put back.
5. Restore writes the recorded baseline and stops claiming to govern anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from .backend import PowerBackend, drift, read_all
from .errors import ActionRefused, ApplyFailed, BackendError, QuietGovernorError
from .planning import (
    Baseline,
    Change,
    Plan,
    Readings,
    plan_for_writes,
    utc_now,
)
from .policy import (
    BUILTIN_PROFILES,
    DEFAULT_SCOPE,
    POWER_SOURCES,
    SETTINGS,
    SCOPES,
    WRITE_ORDER,
    Profile,
    build_profile,
    validate_scope,
)
from .store import AppliedState, StateStore

#: Called with the finished plan; returning False cancels the operation.
Confirmer = Callable[[Plan], bool]

RESTORE_PROFILE_NAME = "restore"


@dataclass(frozen=True)
class ApplyResult:
    plan: Plan
    applied: tuple[Change, ...]
    verified: bool
    baseline_created: bool
    baseline_path: str
    dry_run: bool

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def _with_notes(plan: Plan, notes: list[str]) -> Plan:
    return Plan(
        profile=plan.profile,
        scheme_guid=plan.scheme_guid,
        scheme_name=plan.scheme_name,
        changes=plan.changes,
        unchanged=plan.unchanged,
        checks=plan.checks,
        notes=tuple(dict.fromkeys(notes)),
    )


@dataclass(frozen=True)
class Status:
    scheme_guid: str
    scheme_name: str
    state: AppliedState | None
    readings: Readings
    baseline: Baseline | None
    #: (setting, source, live value, managed value) for values that drifted.
    drift: tuple[tuple[str, str, int | None, int], ...]

    @property
    def governing(self) -> bool:
        return self.state is not None and self.state.is_governing


class Controller:
    """Ties a backend and a state store together into user-level operations."""

    def __init__(self, backend: PowerBackend, store: StateStore) -> None:
        self.backend = backend
        self.store = store

    # --- profiles ----------------------------------------------------------

    def profiles(self) -> dict[str, Profile]:
        """Built-in profiles overlaid with the user's saved profiles."""
        out = dict(BUILTIN_PROFILES)
        for name, raw in self.store.load_user_profiles().items():
            try:
                out[name] = Profile.from_dict(raw)
            except ValueError as exc:
                raise QuietGovernorError(
                    f"The saved profile {name!r} in {self.store.profiles_path} is "
                    f"not valid: {exc}. Delete that profile with "
                    f"'quietgovernor profile delete {name}' or fix the file."
                ) from exc
        return out

    def get_profile(self, name: str) -> Profile:
        profile = self.profiles().get(name)
        if profile is None:
            available = ", ".join(sorted(self.profiles()))
            raise QuietGovernorError(
                f"There is no profile called {name!r}. Available profiles: "
                f"{available}. Use 'quietgovernor profiles' to see them."
            )
        return profile

    def save_profile(
        self,
        name: str,
        values: Mapping[str, Mapping[str, int]],
        *,
        description: str = "",
        scope: str = DEFAULT_SCOPE,
    ) -> Profile:
        """Save (or replace) a user profile that overrides only ``values``.

        Anything not named keeps whatever the PC currently has, which is why
        these profiles are planned against live readings before use.
        """
        profile = build_profile(
            name, description or f"Your saved '{name}' profile.", values, scope=scope, partial=True
        )
        profile = Profile(
            name=profile.name,
            description=profile.description,
            values=profile.values,
            scope=profile.scope,
            partial=True,
            origin="user",
        )
        stored = self.store.load_user_profiles()
        stored[name] = profile.to_dict()
        self.store.save_user_profiles(stored)
        return profile

    def delete_profile(self, name: str) -> None:
        if name in BUILTIN_PROFILES:
            raise QuietGovernorError(
                f"{name!r} is a built-in profile and cannot be deleted. "
                "You can overwrite your own profiles by saving a new one with "
                "the same name."
            )
        stored = self.store.load_user_profiles()
        if name not in stored:
            raise QuietGovernorError(
                f"There is no saved profile called {name!r}. Built-in profiles "
                f"({', '.join(sorted(BUILTIN_PROFILES))}) are always available."
            )
        del stored[name]
        self.store.save_user_profiles(stored)

    def _materialise(self, profile: Profile, readings: Readings) -> Profile:
        """Fill a partial profile's gaps from the PC's current values.

        A partial profile (from ``profile set``) only names some settings.
        Everything else must be left exactly as it is, so the missing values
        are taken from the live readings at plan time.
        """
        if not profile.partial:
            return profile
        values: dict[str, dict[str, int]] = {}
        inherited: set[tuple[str, str]] = set()
        for key in WRITE_ORDER:
            for source in profile.sources:
                target = profile.value_for(key, source)
                if target is not None:
                    values.setdefault(key, {})[source] = target
                    continue
                reading = readings.get(key)
                current = reading.value_for(source) if reading is not None else None
                if current is None:
                    # Unavailable settings are skipped by planning; leaving
                    # them out keeps the plan honest about doing nothing.
                    continue
                setting = SETTINGS[key]
                if setting.minimum <= current <= setting.maximum:
                    values.setdefault(key, {})[source] = current
                    inherited.add((key, source))
        return Profile(
            name=profile.name,
            description=profile.description,
            values=values,
            scope=profile.scope,
            partial=False,
            origin=profile.origin,
            inherited=frozenset(inherited),
        )

    def _restore_profile_from(self, baseline: Baseline) -> Profile:
        """A resolved profile whose values are the recorded baseline."""
        values: dict[str, dict[str, int]] = {}
        for key in WRITE_ORDER:
            reading = baseline.readings.get(key)
            if reading is None or not reading.available:
                continue
            for source in POWER_SOURCES:
                value = reading.value_for(source)
                if value is None:
                    continue
                values.setdefault(key, {})[source] = value
        return Profile(
            name=RESTORE_PROFILE_NAME,
            description="Put every managed setting back to its recorded original value.",
            values=values,
            scope="both",
            partial=False,
            origin="baseline",
        )

    # --- planning ----------------------------------------------------------

    def plan(
        self,
        profile_name: str,
        *,
        scope: str | None = None,
        readings: Readings | None = None,
    ) -> Plan:
        """Plan a profile (or ``restore``) without changing anything."""
        if scope is not None:
            validate_scope(scope)
        scheme_guid, scheme_name = self.backend.active_scheme()
        live = readings if readings is not None else read_all(self.backend)
        if profile_name == RESTORE_PROFILE_NAME:
            baseline = self.store.load_baseline()
            if baseline is None:
                raise QuietGovernorError(
                    "There is no restore point on this PC, which means "
                    "QuietGovernor has not changed any settings here yet, so "
                    "there is nothing to restore. Run "
                    "'quietgovernor apply --profile quiet' first if you want a "
                    "restore point created."
                )
            profile = self._restore_profile_from(baseline)
            notes = []
            if baseline.scheme_guid and baseline.scheme_guid != scheme_guid:
                notes.append(
                    f"The original values were recorded in power plan "
                    f"'{baseline.scheme_name}', but '{scheme_name}' is active now. "
                    f"Restore writes the recorded values into the active plan; "
                    f"the other plan is not modified."
                )
            return plan_for_writes(profile, scheme_guid, scheme_name, live, notes)
        profile = self.get_profile(profile_name)
        if scope and scope != profile.scope:
            profile = self._with_scope(profile, scope)
        materialised = self._materialise(profile, live)
        return plan_for_writes(materialised, scheme_guid, scheme_name, live)

    def _with_scope(self, profile: Profile, scope: str) -> Profile:
        """Re-target a profile at other power sources.

        Values missing for the newly included source are left out and picked
        up from the live readings by :meth:`_materialise`; values for sources
        that left the scope are dropped.
        """
        wanted = SCOPES[scope]
        values: dict[str, dict[str, int]] = {}
        for key, per_source in profile.values.items():
            kept = {source: value for source, value in per_source.items() if source in wanted}
            if kept:
                values[key] = kept
        return Profile(
            name=profile.name,
            description=profile.description,
            values=values,
            scope=scope,
            partial=True,
            origin=profile.origin,
        )

    def captures_baseline(self) -> bool:
        return self.store.load_baseline() is not None

    # --- applying ----------------------------------------------------------

    def apply(
        self,
        profile_name: str,
        *,
        scope: str | None = None,
        dry_run: bool = False,
        confirm: Confirmer | None = None,
    ) -> ApplyResult:
        """Apply ``profile_name``, with baseline capture and rollback.

        Order matters and is deliberate:

        1. read the machine and plan;
        2. show the plan and get confirmation (nothing is written yet);
        3. read again, and stop if anything changed while the user decided;
        4. record the baseline if this is the first change ever;
        5. write, verify, roll back on failure, and record state.
        """
        if profile_name == RESTORE_PROFILE_NAME:
            return self.restore(dry_run=dry_run, confirm=confirm)

        scheme_guid, scheme_name = self.backend.active_scheme()
        readings = read_all(self.backend)
        plan = self.plan(profile_name, scope=scope, readings=readings)

        existing_baseline = self.store.load_baseline()
        notes = list(plan.notes)
        if existing_baseline is None and dry_run and not plan.is_empty:
            notes.append(
                "A preview makes no changes and therefore records no restore "
                "point; 'apply' records one before its first change."
            )
        if existing_baseline is not None and existing_baseline.scheme_guid != scheme_guid:
            notes.append(
                f"Your original settings were recorded in power plan "
                f"'{existing_baseline.scheme_name}', but '{scheme_name}' is "
                f"active now. The restore point is kept as it was; 'restore' "
                f"writes it into the active plan."
            )
        if notes != list(plan.notes):
            plan = _with_notes(plan, notes)

        if dry_run:
            return ApplyResult(
                plan=plan,
                applied=(),
                verified=False,
                baseline_created=False,
                baseline_path=str(self.store.baseline_path),
                dry_run=True,
            )

        if plan.is_empty:
            # Nothing was written, so nothing is being governed; recording
            # state here would later let 'restore' claim it had work to undo.
            return ApplyResult(
                plan=plan,
                applied=(),
                verified=True,
                baseline_created=False,
                baseline_path=str(self.store.baseline_path),
                dry_run=False,
            )

        if confirm is not None and not confirm(plan):
            raise ActionRefused(
                "Nothing was changed. You can review the plan again with "
                f"'quietgovernor plan --profile {profile_name}'."
            )

        fresh = self._verify_unchanged(plan, readings)

        baseline_created = False
        if existing_baseline is None:
            baseline = Baseline(
                scheme_guid=scheme_guid,
                scheme_name=scheme_name,
                readings=fresh,
                captured_at=utc_now(),
            )
            self.store.ensure_baseline(baseline)
            baseline_created = True

        applied = self._write_and_verify(plan.changes, plan)
        self._record_state(plan, profile_name)
        return ApplyResult(
            plan=plan,
            applied=applied,
            verified=True,
            baseline_created=baseline_created,
            baseline_path=str(self.store.baseline_path),
            dry_run=False,
        )

    def restore(self, *, dry_run: bool = False, confirm: Confirmer | None = None) -> ApplyResult:
        """Write the recorded baseline back and stop governing."""
        baseline = self.store.load_baseline()
        if baseline is None:
            raise QuietGovernorError(
                "There is no restore point on this PC, which means "
                "QuietGovernor has not changed any settings here, so there is "
                "nothing to undo."
            )
        readings = read_all(self.backend)
        plan = self.plan(RESTORE_PROFILE_NAME, readings=readings)

        if dry_run:
            return ApplyResult(
                plan=plan,
                applied=(),
                verified=False,
                baseline_created=False,
                baseline_path=str(self.store.baseline_path),
                dry_run=True,
            )
        if plan.is_empty:
            self.store.clear_state()
            return ApplyResult(
                plan=plan,
                applied=(),
                verified=True,
                baseline_created=False,
                baseline_path=str(self.store.baseline_path),
                dry_run=False,
            )
        if confirm is not None and not confirm(plan):
            raise ActionRefused(
                "Nothing was changed. Your current settings and the restore "
                "point are both still in place."
            )
        self._verify_unchanged(plan, readings)
        applied = self._write_and_verify(plan.changes, plan)
        self.store.clear_state()
        return ApplyResult(
            plan=plan,
            applied=applied,
            verified=True,
            baseline_created=False,
            baseline_path=str(self.store.baseline_path),
            dry_run=False,
        )

    def _verify_unchanged(self, plan: Plan, readings: Readings) -> Readings:
        """Re-read after confirmation and stop if the world moved.

        Something else (the Windows user, a driver reset, another tuning tool)
        may have changed a setting, or switched power plan, while the question
        was on screen. Writing a plan built on stale readings could record a
        baseline that was never really the original, so this aborts instead.
        """
        scheme_guid, scheme_name = self.backend.active_scheme()
        if scheme_guid != plan.scheme_guid:
            raise BackendError(
                f"The active power plan changed from '{plan.scheme_name}' to "
                f"'{scheme_name}' while this plan was waiting for "
                "confirmation, so nothing was written. Run the command again "
                "to review a plan for the plan that is active now."
            )
        fresh = read_all(self.backend)
        if dict(fresh) != dict(readings):
            raise BackendError(
                "Your power settings changed while this plan was waiting for "
                "confirmation, so nothing was written. Run the command again to "
                "review an up-to-date plan."
            )
        return fresh

    def _write_and_verify(self, changes: tuple[Change, ...], plan: Plan) -> tuple[Change, ...]:
        """Write every change, verify it, and undo everything on any failure."""
        ensure_writable = getattr(self.backend, "ensure_writable", None)
        if callable(ensure_writable):
            # Check permission before the first write, so a refusal happens
            # with nothing to roll back.
            ensure_writable()
        written: list[Change] = []
        try:
            for change in changes:
                self.backend.write(change.setting_key, change.source, change.target)
                written.append(change)
                reading = self.backend.read(change.setting_key)
                got = reading.value_for(change.source)
                if got != change.target:
                    raise BackendError(
                        f"Windows reported {SETTINGS[change.setting_key].label} as "
                        f"{got} after setting it to {change.target}."
                    )
        except BackendError as exc:
            undone, remaining = self._rollback(written)
            if remaining:
                raise ApplyFailed(
                    f"{exc} QuietGovernor tried to undo its changes and could "
                    f"not restore {remaining}. Your original values are saved in "
                    f"{self.store.baseline_path}; run 'quietgovernor restore' to "
                    f"put them back.",
                    rolled_back=False,
                ) from exc
            raise ApplyFailed(
                f"{exc} No settings were left changed: the {undone} setting(s) "
                "already written were put back to their original values.",
                rolled_back=True,
            ) from exc
        return tuple(written)

    def _rollback(self, written: list[Change]) -> tuple[str, str]:
        remaining: list[str] = []
        undone = 0
        for change in reversed(written):
            if change.current is None:
                continue
            try:
                self.backend.write(change.setting_key, change.source, change.current)
                undone += 1
            except BackendError:
                remaining.append(
                    f"{SETTINGS[change.setting_key].label} on {change.source} power"
                )
        return (f"{undone}", ", ".join(remaining))

    def _record_state(self, plan: Plan, profile_name: str) -> None:
        values: dict[str, dict[str, int]] = {}
        for change in (*plan.changes, *plan.unchanged):
            values.setdefault(change.setting_key, {})[change.source] = change.target
        state = AppliedState(
            profile_name=profile_name,
            managed_values=values,
            scheme_guid=plan.scheme_guid,
            scheme_name=plan.scheme_name,
            profile_origin=plan.profile.origin,
        )
        self.store.save_state(state)

    # --- reporting ---------------------------------------------------------

    def status(self) -> Status:
        scheme_guid, scheme_name = self.backend.active_scheme()
        readings = read_all(self.backend)
        state = self.store.load_state()
        baseline = self.store.load_baseline()
        drifted = (
            tuple(drift(readings, state.managed_values))
            if state is not None and state.is_governing
            else ()
        )
        return Status(
            scheme_guid=scheme_guid,
            scheme_name=scheme_name,
            state=state,
            readings=readings,
            baseline=baseline,
            drift=drifted,
        )
