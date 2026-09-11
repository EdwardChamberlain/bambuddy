"""Eligibility matcher for Slicer Pipeline runs (#1425 PR B).

Given a pipeline + the user's pinned target printer, this returns a structured
report of issues the operator should resolve before running. The frontend
displays the report; the user can ``Run anyway`` to proceed (lenient policy —
the print may still fail at the printer, but Bambuddy isn't going to refuse
the click).

Issue kinds (pinned for tests + i18n keys):
  - printer_not_set         — pipeline has no target_printer_id
  - printer_not_found       — target_printer_id points at a deleted/missing row
  - printer_disabled        — Printer.is_active is False (#1476)
  - printer_offline         — MQTT not connected
  - filament_type_mismatch  — AMS slot loaded with wrong filament type
  - filament_color_mismatch — type matches, colour differs
  - ams_slot_missing        — pipeline expects N filament slots but AMS exposes fewer
  - filament_unverified     — pipeline filament preset is a non-local tier we
                              can't statically read (cloud / orca_cloud / standard);
                              the run will proceed, but the operator should
                              double-check

The matcher is a pure-ish function over (pipeline, printer row, live AMS state,
local-preset dict) so unit tests can drive it with fixtures without spinning up
MQTT. The route handler is the only place that talks to ``printer_manager``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.local_preset import LocalPreset
from backend.app.models.printer import Printer
from backend.app.models.slicer_pipeline import SlicerPipeline

IssueKind = Literal[
    "printer_not_set",
    "printer_not_found",
    "printer_disabled",
    "printer_offline",
    "filament_type_mismatch",
    "filament_color_mismatch",
    "ams_slot_missing",
    "filament_unverified",
]


@dataclass(frozen=True)
class EligibilityIssue:
    kind: IssueKind
    slot_index: int | None = None
    expected: str | None = None
    actual: str | None = None


@dataclass(frozen=True)
class EligibilityReport:
    ok: bool
    target_printer_id: int | None
    target_printer_name: str | None
    issues: tuple[EligibilityIssue, ...]


# Same equivalence map as print_scheduler._canonical_filament_type but kept
# local so this module has no upward dependency on the scheduler. Mirrors the
# scheduler's behaviour: BBL-prefixed product names normalise to the base type
# (e.g. "PLA Basic" → "PLA"). When the scheduler's map gets a new alias, this
# one needs the same entry.
_FILAMENT_EQUIV_MAP = {
    "PLA": "PLA",
    "PLA BASIC": "PLA",
    "PLA MATTE": "PLA",
    "PLA SILK": "PLA",
    "PLA PRO": "PLA",
    "PLA TOUGH": "PLA",
    "PETG": "PETG",
    "PETG HF": "PETG",
    "PETG BASIC": "PETG",
    "PETG TRANSLUCENT": "PETG",
    "ABS": "ABS",
    "ASA": "ASA",
    "TPU": "TPU",
    "TPU 95A": "TPU",
    "PC": "PC",
    "PA": "PA",
    "PA-CF": "PA",
    "PVA": "PVA",
}


def _canonical(ftype: str) -> str:
    upper = (ftype or "").strip().upper()
    return _FILAMENT_EQUIV_MAP.get(upper, upper)


def _normalise_colour(colour: str | None) -> str:
    if not colour:
        return ""
    return colour.replace("#", "").lower()[:6]


def _ams_slots(raw_data: dict) -> list[tuple[str, str]]:
    """Flatten AMS + external spool into ``[(type, colour_hex6), ...]`` in slot
    order. Uses the same field shape as print_scheduler._check_required_filaments.
    """
    out: list[tuple[str, str]] = []
    for ams_unit in raw_data.get("ams") or []:
        for tray in ams_unit.get("tray") or []:
            tray_type = tray.get("tray_type") or ""
            tray_colour = tray.get("tray_color") or ""
            out.append((_canonical(tray_type), _normalise_colour(tray_colour)))
    for vt in raw_data.get("vt_tray") or []:
        vt_type = vt.get("tray_type") or ""
        vt_colour = vt.get("tray_color") or ""
        out.append((_canonical(vt_type), _normalise_colour(vt_colour)))
    return out


async def _expected_filament(
    db: AsyncSession,
    source: str,
    preset_id: str,
) -> tuple[str | None, str | None]:
    """Return ``(canonical_type, normalised_colour)`` for a pipeline filament
    slot's PresetRef, or ``(None, None)`` when the preset can't be resolved
    statically (cloud / orca_cloud / standard — read at slice time, not here).
    """
    if source != "local":
        # Cloud / orca_cloud / standard: surface as ``filament_unverified``
        # in the report, the matcher decides.
        return (None, None)
    try:
        local_id = int(preset_id)
    except (TypeError, ValueError):
        return (None, None)
    row = (await db.execute(select(LocalPreset).where(LocalPreset.id == local_id))).scalar_one_or_none()
    if row is None:
        return (None, None)
    return (_canonical(row.filament_type or ""), _normalise_colour(row.default_filament_colour))


async def check_pipeline_eligibility(
    db: AsyncSession,
    pipeline: SlicerPipeline,
    printer_raw_status: dict | None,
) -> EligibilityReport:
    """Build the report. ``printer_raw_status`` is the live ``PrinterState``
    serialised to a dict (``connected``, ``raw_data``), or ``None`` when the
    target printer has no MQTT client. The route handler does the
    ``printer_manager.get_status`` lookup so this stays unit-testable.
    """
    issues: list[EligibilityIssue] = []

    # 1. Target printer set?
    if pipeline.target_printer_id is None:
        issues.append(EligibilityIssue(kind="printer_not_set"))
        return EligibilityReport(
            ok=False,
            target_printer_id=None,
            target_printer_name=None,
            issues=tuple(issues),
        )

    printer = (await db.execute(select(Printer).where(Printer.id == pipeline.target_printer_id))).scalar_one_or_none()
    if printer is None:
        issues.append(EligibilityIssue(kind="printer_not_found"))
        return EligibilityReport(
            ok=False,
            target_printer_id=pipeline.target_printer_id,
            target_printer_name=None,
            issues=tuple(issues),
        )

    target_name = printer.name

    # 2. Disabled?
    if not printer.is_active:
        issues.append(EligibilityIssue(kind="printer_disabled"))

    # 3. Offline?
    if not printer_raw_status or not printer_raw_status.get("connected"):
        issues.append(EligibilityIssue(kind="printer_offline"))
        # Without live AMS state, skip slot checks — would surface as a
        # cascade of misleading mismatches.
        return EligibilityReport(
            ok=not issues,
            target_printer_id=printer.id,
            target_printer_name=target_name,
            issues=tuple(issues),
        )

    # 4. Per-slot filament match.
    try:
        filament_refs = json.loads(pipeline.filament_presets_json or "[]")
    except (json.JSONDecodeError, TypeError):
        filament_refs = []

    ams_slots = _ams_slots(printer_raw_status.get("raw_data") or {})

    for slot_index, ref in enumerate(filament_refs):
        if not isinstance(ref, dict):
            continue
        source = ref.get("source", "")
        preset_id = ref.get("id", "")
        expected_type, expected_colour = await _expected_filament(db, source, str(preset_id))

        if expected_type is None:
            issues.append(
                EligibilityIssue(
                    kind="filament_unverified",
                    slot_index=slot_index,
                    expected=f"{source}:{preset_id}",
                )
            )
            continue

        if slot_index >= len(ams_slots):
            issues.append(
                EligibilityIssue(
                    kind="ams_slot_missing",
                    slot_index=slot_index,
                    expected=expected_type,
                )
            )
            continue

        actual_type, actual_colour = ams_slots[slot_index]
        if expected_type and actual_type and expected_type != actual_type:
            issues.append(
                EligibilityIssue(
                    kind="filament_type_mismatch",
                    slot_index=slot_index,
                    expected=expected_type,
                    actual=actual_type or "(empty)",
                )
            )
            continue
        if expected_colour and actual_colour and expected_colour != actual_colour:
            issues.append(
                EligibilityIssue(
                    kind="filament_color_mismatch",
                    slot_index=slot_index,
                    expected=expected_colour,
                    actual=actual_colour,
                )
            )

    # Unverified filament refs are surfaced as INFO — they don't flip ok=False.
    # The user is told what we couldn't verify so they can sanity-check
    # before pulling the trigger, but the lenient policy doesn't refuse to
    # let them run.
    blocking_issues = [i for i in issues if i.kind != "filament_unverified"]

    return EligibilityReport(
        ok=not blocking_issues,
        target_printer_id=printer.id,
        target_printer_name=target_name,
        issues=tuple(issues),
    )
