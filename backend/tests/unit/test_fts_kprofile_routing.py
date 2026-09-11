"""FTS inlet bindings resolve AMS slots to the nozzle they currently feed."""

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
