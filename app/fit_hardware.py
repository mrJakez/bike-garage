"""Small FIT ``device_info`` extractor used for resolver hardware evidence.

We deliberately decode only device metadata here.  Ride samples, maps and
vendor developer fields stay with the original FIT artifact and are outside
the resolver's identity pipeline.
"""
from __future__ import annotations

import struct
from typing import Any


_BASE_TYPES = {
    0: ("enum", 1), 1: ("sint8", 1), 2: ("uint8", 1), 7: ("string", 1),
    10: ("uint8z", 1), 13: ("byte", 1), 131: ("sint16", 2),
    132: ("uint16", 2), 133: ("sint32", 4), 134: ("uint32", 4),
    139: ("uint16z", 2), 140: ("uint32z", 4),
}
_FORMATS = {
    "enum": "B", "uint8": "B", "uint8z": "B", "byte": "B", "sint8": "b",
    "uint16": "H", "uint16z": "H", "sint16": "h", "uint32": "I",
    "uint32z": "I", "sint32": "i",
}
_MANUFACTURERS = {20: "CardioSport", 63: "Specialized", 268: "SRAM", 289: "Hammerhead"}
_DEVICE_TYPES = {11: "bike_power", 34: "shifting"}
_BATTERY_STATUS = {1: "new", 2: "good", 3: "ok", 4: "low", 5: "critical", 7: "unknown"}


def _decode(raw: bytes, base_type: int, architecture: int) -> Any:
    name, width = _BASE_TYPES.get(base_type, ("byte", 1))
    if name == "string":
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    if len(raw) != width or name not in _FORMATS:
        return raw.hex()
    return struct.unpack(("<" if architecture == 0 else ">") + _FORMATS[name], raw)[0]


def extract_device_info(fit_bytes: bytes) -> list[dict[str, Any]]:
    """Return resolver-relevant ANT+ observations from FIT ``device_info``.

    FIT global message 23 stores both the recording computer and connected
    sensors.  We retain unknown device types too, but only classify the types
    we can confidently resolve today.
    """
    if len(fit_bytes) < 14 or fit_bytes[8:12] != b".FIT":
        raise ValueError("Not a FIT file")
    offset = fit_bytes[0]
    end = len(fit_bytes) - 2  # FIT CRC
    definitions: dict[int, tuple[int, int, list[tuple[int, int, int]], list[tuple[int, int, int]]]] = {}
    observations: list[dict[str, Any]] = []
    while offset < end:
        header = fit_bytes[offset]; offset += 1
        if header & 0x80:  # compressed timestamp header
            local = (header >> 5) & 0x03
            definition = definitions.get(local)
            if definition is None:
                break
        else:
            local = header & 0x0F
            if header & 0x40:
                offset += 1  # reserved
                architecture = fit_bytes[offset]; offset += 1
                endian = "<" if architecture == 0 else ">"
                global_number = struct.unpack_from(endian + "H", fit_bytes, offset)[0]; offset += 2
                field_count = fit_bytes[offset]; offset += 1
                fields = [tuple(fit_bytes[offset + index * 3:offset + index * 3 + 3]) for index in range(field_count)]
                offset += field_count * 3
                developer_fields: list[tuple[int, int, int]] = []
                if header & 0x20:
                    developer_count = fit_bytes[offset]; offset += 1
                    developer_fields = [tuple(fit_bytes[offset + index * 3:offset + index * 3 + 3]) for index in range(developer_count)]
                    offset += developer_count * 3
                definitions[local] = (global_number, architecture, fields, developer_fields)
                continue
            definition = definitions.get(local)
            if definition is None:
                break
        global_number, architecture, fields, developer_fields = definition
        values: dict[int, Any] = {}
        for number, size, base_type in fields:
            values[number] = _decode(fit_bytes[offset:offset + size], base_type, architecture)
            offset += size
        for _, size, _ in developer_fields:
            offset += size
        if global_number != 23:
            continue
        # 21 is ant_device_number, 25 is source_type (1 = ANT+).  A device
        # number is the stable observation we can match against a bike.
        ant_number = values.get(21)
        if not isinstance(ant_number, int) or ant_number in (0, 0xFFFF):
            continue
        device_type = values.get(1)
        component_type = _DEVICE_TYPES.get(device_type, "unknown")
        observations.append({
            "component_type": component_type,
            "protocol": "ANT_PLUS",
            "ant_device_number": ant_number,
            "manufacturer_id": values.get(2),
            "manufacturer_name": _MANUFACTURERS.get(values.get(2), str(values.get(2) or "Unknown")),
            "device_type_id": device_type,
            "serial_number": values.get(3) if values.get(3) not in (0, 0xFFFFFFFF) else None,
            "product_id": values.get(4) if values.get(4) not in (0xFFFF,) else None,
            "product_name": values.get(27) or "",
            "battery_status": _BATTERY_STATUS.get(values.get(11), "unknown"),
            "observed_at": values.get(253),
            "raw": values,
        })
    return observations
