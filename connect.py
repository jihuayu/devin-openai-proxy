"""Minimal Connect-RPC streaming helpers.

Supports both text/JSON and raw protobuf frames.
"""

import json
import struct
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import httpx


class ConnectError(Exception):
    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


def encode_connect_request(payload: bytes, flags: int = 0) -> bytes:
    """Wrap a single protobuf/JSON message in a Connect streaming frame."""
    return struct.pack(">B", flags) + struct.pack(">I", len(payload)) + payload


async def iter_connect_frames(
    response: httpx.Response,
) -> AsyncIterator[Tuple[int, Optional[bytes]]]:
    """Yield (flags, raw_payload) for each Connect streaming frame."""
    buffer = b""
    async for chunk in response.aiter_raw():
        buffer += chunk
        while True:
            if len(buffer) < 5:
                break
            flags = buffer[0]
            length = struct.unpack(">I", buffer[1:5])[0]
            if len(buffer) < 5 + length:
                break
            payload = buffer[5 : 5 + length]
            buffer = buffer[5 + length :]
            yield flags, payload


async def iter_connect_json_frames(
    response: httpx.Response,
) -> AsyncIterator[Tuple[int, Optional[Dict[str, Any]]]]:
    """Yield (flags, json_object) for JSON payloads."""
    async for flags, payload in iter_connect_frames(response):
        if flags & 0x01:
            yield flags, None
            continue
        try:
            obj = json.loads(payload.decode("utf-8")) if payload else None
        except Exception:
            obj = None
        yield flags, obj
