"""Static geometry of the lot. Slot ids are A1..A6, B1..B6 (one ESP32 node per zone)."""
import config


def build_layout(zones=None, per_zone=None):
    zones = zones or config.ZONES
    per_zone = per_zone or config.SLOTS_PER_ZONE
    slots = []
    for zi, z in enumerate(zones):
        for i in range(per_zone):
            slots.append({
                "id": f"{z}{i + 1}",
                "zone": z,
                "index": i,
                "row": zi,
                # walking/driving distance from the entry gate in metres (used for guidance)
                "gate_distance_m": round(18 + i * 3.0 + zi * 1.5, 1),
            })
    return slots
