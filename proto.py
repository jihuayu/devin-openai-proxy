"""Minimal protobuf wire-format encoder/decoder.

This is schema-agnostic: callers provide field schemas for encoding and
interpret the decoded (field_num -> list of (wire_type, raw_value)) tree.

Wire types:
    0  varint
    1  fixed64 (int64/uint64/sfixed64/sint64/double)
    2  length-delimited (string/bytes/submessage/packed)
    5  fixed32 (int32/uint32/sfixed32/sint32/float)
"""

import struct
from typing import Any, Dict, List, Optional, Tuple, Union


class ProtoParseError(Exception):
    pass


def encode_varint(value: int) -> bytes:
    """Encode an unsigned varint."""
    if value < 0:
        value &= 0xFFFFFFFFFFFFFFFF
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def encode_zigzag(value: int, bits: int = 64) -> bytes:
    """Encode a signed integer with zigzag."""
    if value >= 0:
        zigzag = value * 2
    else:
        zigzag = (-value) * 2 - 1
    if bits == 32:
        zigzag &= 0xFFFFFFFF
    return encode_varint(zigzag)


def encode_tag(field_num: int, wire_type: int) -> bytes:
    return encode_varint((field_num << 3) | (wire_type & 7))


def decode_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """Decode a varint from data at pos.  Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoParseError("varint overflow")
    raise ProtoParseError("truncated varint")


def _encode_single_value(value: Any, wire_type: int) -> bytes:
    if wire_type == 0:
        if isinstance(value, bool):
            return encode_varint(1 if value else 0)
        return encode_varint(int(value))
    if wire_type == 1:
        # fixed64 / double
        if isinstance(value, float):
            return struct.pack("<d", value)
        return struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)
    if wire_type == 2:
        if isinstance(value, bytes):
            payload = value
        elif isinstance(value, str):
            payload = value.encode("utf-8")
        else:
            raise TypeError(f"wire type 2 value must be bytes or str, got {type(value)}")
        return encode_varint(len(payload)) + payload
    if wire_type == 5:
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    raise ProtoParseError(f"unsupported wire type {wire_type}")


def encode_field(field_num: int, wire_type: int, value: Any) -> bytes:
    """Encode a single field (non-repeated)."""
    return encode_tag(field_num, wire_type) + _encode_single_value(value, wire_type)


def encode_repeated_field(field_num: int, wire_type: int, values: List[Any]) -> bytes:
    """Encode a repeated field as individual tagged entries."""
    result = bytearray()
    for value in values:
        result.extend(encode_field(field_num, wire_type, value))
    return bytes(result)


def encode_message(schema: Dict[int, Union[int, Tuple[int, bool], List[int]]],
                   values: Dict[int, Any]) -> bytes:
    """Encode a protobuf message using a schema.

    ``schema`` maps field_number -> wire_type (int) or (wire_type, repeated_bool).
    ``values`` maps field_number -> value or list of values.

    Submessages must be pre-encoded as bytes.
    """
    result = bytearray()
    for field_num, value in values.items():
        spec = schema.get(field_num)
        if spec is None:
            continue
        if isinstance(spec, tuple):
            wire_type, repeated = spec
        elif isinstance(spec, list):
            # e.g. [wire_type, repeated]
            wire_type, repeated = int(spec[0]), bool(spec[1]) if len(spec) > 1 else False
        else:
            wire_type = int(spec)
            repeated = isinstance(value, (list, tuple)) and not isinstance(value, bytes)

        if repeated:
            for item in value:
                result.extend(encode_field(field_num, wire_type, item))
        else:
            result.extend(encode_field(field_num, wire_type, value))
    return bytes(result)


def decode_message(data: bytes) -> Dict[int, List[Tuple[int, Any]]]:
    """Decode a protobuf message into a dict of field numbers -> entries.

    Each entry is ``(wire_type, raw_value)``.  For wire type 2, ``raw_value``
    is the raw bytes (callers can decode strings with ``.decode()`` or parse
    submessages with another ``decode_message``).  For wire type 1, value is
    the 8 raw little-endian bytes.  For wire type 5, the 4 raw bytes.
    """
    result: Dict[int, List[Tuple[int, Any]]] = {}
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 7

        if wire_type == 0:
            value, pos = decode_varint(data, pos)
        elif wire_type == 1:
            if pos + 8 > n:
                raise ProtoParseError("truncated fixed64")
            value = data[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = decode_varint(data, pos)
            if pos + length > n:
                raise ProtoParseError("truncated length-delimited")
            value = data[pos : pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > n:
                raise ProtoParseError("truncated fixed32")
            value = data[pos : pos + 4]
            pos += 4
        elif wire_type in (3, 4):
            raise ProtoParseError(f"deprecated group wire type {wire_type}")
        else:
            raise ProtoParseError(f"unknown wire type {wire_type}")

        result.setdefault(field_num, []).append((wire_type, value))

    return result


def get_string(entry: Tuple[int, Any]) -> str:
    """Return a wire-type-2 string as a Python str."""
    wire, value = entry
    if wire != 2:
        raise ValueError("not a length-delimited field")
    return value.decode("utf-8") if isinstance(value, bytes) else value


def get_varint(entry: Tuple[int, Any]) -> int:
    wire, value = entry
    if wire != 0:
        raise ValueError("not a varint field")
    return int(value)


def get_double(entry: Tuple[int, Any]) -> float:
    """Interpret a fixed64 entry as a little-endian double."""
    wire, value = entry
    if wire == 1 and isinstance(value, bytes) and len(value) == 8:
        return struct.unpack("<d", value)[0]
    raise ValueError("not a fixed64/double field")


def get_submessage(entry: Tuple[int, Any]) -> bytes:
    wire, value = entry
    if wire != 2:
        raise ValueError("not a length-delimited submessage")
    return value


def decode_string(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")
