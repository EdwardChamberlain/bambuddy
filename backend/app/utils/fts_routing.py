"""Resolve the nozzle fed by an AMS slot, including Filament Track Switches."""

FTS_INLET_EXTRUDER: dict[str, int] = {
    "A": 1,  # left / deputy
    "B": 0,  # right / main
}


def extruder_for_inlet(inlet: str | None) -> int | None:
    """Return the extruder fed by switch inlet A or B."""
    if not inlet:
        return None
    return FTS_INLET_EXTRUDER.get(inlet.upper())


def slot_extruder(
    ams_id: int,
    tray_id: int,
    ams_extruder_map: dict | None,
    ams_switch_inlet: dict | None = None,
) -> int | None:
    """Resolve an AMS slot to its extruder, or None when it is unknown."""
    if ams_id == 255:
        return 1 - tray_id if tray_id in (0, 1) else None

    if ams_extruder_map:
        mapped = ams_extruder_map.get(str(ams_id))
        if mapped is not None:
            return int(mapped)

    if ams_switch_inlet:
        return extruder_for_inlet(ams_switch_inlet.get(str(ams_id)))

    return None
