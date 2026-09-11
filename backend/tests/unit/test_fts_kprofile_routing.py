"""FTS inlet bindings resolve AMS slots to the nozzle they currently feed."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.utils.fts_routing import FTS_INLET_EXTRUDER, extruder_for_inlet, slot_extruder


def test_fts_inlets_map_to_distinct_extruders():
    assert extruder_for_inlet("A") == 1
    assert extruder_for_inlet("B") == 0
    assert sorted(FTS_INLET_EXTRUDER.values()) == [0, 1]


def test_slot_extruder_uses_inlet_when_ams_has_no_direct_binding():
    assert slot_extruder(1, 0, {}, {"1": "B"}) == 0
    assert slot_extruder(2, 0, {}, {"2": "A"}) == 1
    assert slot_extruder(3, 0, {}, {}) is None


def test_fts_move_notifies_only_after_the_first_binding():
    client = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    seen = []
    client.on_fts_inlet_change = lambda ams_id, inlet: seen.append((ams_id, inlet))

    def push(inlet_bits):
        info = f"{(inlet_bits << 24) | (0xE << 8) | 1:08X}"
        client._process_message(
            {
                "print": {
                    "gcode_state": "IDLE",
                    "device": {"fila_switch": {"in": [-1, -1], "out": [1, 1], "stat": 1, "info": 0}},
                    "ams": {"ams": [{"id": "1", "info": info, "tray": []}]},
                }
            }
        )

    push(1)
    push(0)
    push(0)
    push(1)

    assert seen == [(1, "B"), (1, "A")]


def _fts_state():
    state = MagicMock()
    state.raw_data = {
        "ams": [
            {
                "id": "1",
                "tray": [{"id": "0", "tray_type": "PLA", "tray_info_idx": "GFA01", "cali_idx": 16}],
            }
        ]
    }
    return state


def _session(*results):
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=list(results))
    return session


@pytest.mark.asyncio
async def test_fts_move_reapplies_internal_preset_without_a_k_profile():
    """A nozzle-specific filament preset must not depend on a K row existing."""
    from backend.app.main import on_fts_inlet_change

    assignment = MagicMock(tray_id=0, spool_id=12)
    assignment_result = MagicMock()
    assignment_result.scalars.return_value.all.return_value = [assignment]
    spool = MagicMock(id=12)
    spool_result = MagicMock()
    spool_result.scalar_one_or_none.return_value = spool

    client = MagicMock()
    printer_manager = MagicMock()
    printer_manager.get_client.return_value = client
    printer_manager.get_status.return_value = _fts_state()
    reapply = AsyncMock()

    with (
        patch("backend.app.main.printer_manager", printer_manager),
        patch("backend.app.main.async_session", return_value=_session(assignment_result, spool_result)),
        patch("backend.app.services.inventory_mode.spoolman_owns_assignments", new=AsyncMock(return_value=False)),
        patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", new=reapply),
    ):
        await on_fts_inlet_change(7, 1, "A")

    reapply.assert_awaited_once()
    assert reapply.await_args.kwargs["spool"] is spool
    assert reapply.await_args.kwargs["printer_id"] == 7
    assert reapply.await_args.kwargs["ams_id"] == 1
    assert reapply.await_args.kwargs["tray_id"] == 0
    assert reapply.await_args.kwargs["current_tray_info_idx"] == "GFA01"


@pytest.mark.asyncio
async def test_fts_move_reapplies_spoolman_slot_settings():
    from backend.app.main import on_fts_inlet_change

    assignment = MagicMock(tray_id=0, spoolman_spool_id=42)
    assignment_result = MagicMock()
    assignment_result.scalars.return_value.all.return_value = [assignment]
    assign = AsyncMock()
    printer_manager = MagicMock()
    printer_manager.get_client.return_value = MagicMock()
    printer_manager.get_status.return_value = _fts_state()

    with (
        patch("backend.app.main.printer_manager", printer_manager),
        patch("backend.app.main.async_session", return_value=_session(assignment_result)),
        patch("backend.app.services.inventory_mode.spoolman_owns_assignments", new=AsyncMock(return_value=True)),
        patch("backend.app.api.routes.spoolman_inventory.assign_spoolman_slot", new=assign),
    ):
        await on_fts_inlet_change(7, 1, "B")

    assign.assert_awaited_once()
    request = assign.await_args.args[0]
    assert request.spoolman_spool_id == 42
    assert request.printer_id == 7
    assert request.ams_id == 1
    assert request.tray_id == 0
