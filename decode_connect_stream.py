#!/usr/bin/env python3
"""
Decode a Connect-RPC streaming response body or a raw protobuf body.

Usage examples:

  # Decode a raw unary protobuf (e.g. AssignModel response, InferenceRequest)
  python3 decode_connect_stream.py 20250801_120000_exa_api_server_pb_ApiServerService_AssignModel_resp.bin

  # Decode a Connect streaming body (Content-Type: application/connect+proto)
  python3 decode_connect_stream.py --connect 20250801_120000_exa_api_server_pb_ApiServerService_GetDevstralStream_resp.bin

  # Decode a request body (single protobuf, no framing)
  python3 decode_connect_stream.py 20250801_120000_exa_api_server_pb_ApiServerService_GetDevstralStream_req.bin

The script splits Connect envelopes into per-chunk .bin files and then
invokes `protoc --decode_raw` on each chunk. Make sure `protoc` is in PATH.
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


def split_connect_stream(data: bytes, out_dir: Path):
    """Split an application/connect+proto body into individual message payloads."""
    os.makedirs(out_dir, exist_ok=True)
    off = 0
    idx = 0
    while off < len(data):
        if off + 5 > len(data):
            with open(out_dir / f"trailing_{off}.bin", "wb") as f:
                f.write(data[off:])
            print(f"  [!] {len(data) - off} trailing bytes after last envelope")
            break
        flags = data[off]
        length = struct.unpack(">I", data[off + 1 : off + 5])[0]
        off += 5
        payload = data[off : off + length]
        off += length
        with open(out_dir / f"chunk_{idx:04d}_flags_{flags:02x}.bin", "wb") as f:
            f.write(payload)
        # End-of-stream / trailer chunk
        if flags == 0x02:
            print(f"  chunk {idx}: flags=0x02 (end-of-stream), len={length} [trailer/error]")
        else:
            print(f"  chunk {idx}: flags=0x{flags:02x}, len={length}")
        idx += 1
    return idx


def decode_raw(bin_path: Path):
    """Run protoc --decode_raw on a single binary file."""
    try:
        result = subprocess.run(
            ["protoc", "--decode_raw"],
            stdin=bin_path.open("rb"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return result.stdout
        return f"[protoc error] {result.stderr}"
    except FileNotFoundError:
        return "[protoc not found] install `protobuf` (e.g. `brew install protobuf`)"
    except Exception as e:
        return f"[exception] {e}"


def main():
    parser = argparse.ArgumentParser(description="Decode Devin CLI Connect-RPC/protobuf bodies")
    parser.add_argument("file", type=Path, help=".bin file captured by connect_capture.py")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Treat the file as a Connect streaming envelope body",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to dump split chunks (default: tempfile or same as input)",
    )
    args = parser.parse_args()

    data = args.file.read_bytes()
    print(f"Input: {args.file} ({len(data)} bytes)")

    if args.connect:
        out_dir = args.output_dir or args.file.with_suffix("")
        num_chunks = split_connect_stream(data, out_dir)
        print(f"\nSplit into {num_chunks} chunks in {out_dir}\n")

        for chunk in sorted(out_dir.glob("chunk_*.bin")):
            print(f"--- {chunk.name} ---")
            decoded = decode_raw(chunk)
            if decoded.startswith("[protoc") and "flags_02" in chunk.name:
                # End-of-stream / trailer chunk: may be trailers or empty
                raw = chunk.read_bytes()
                print(f"[end-of-stream, raw bytes: {raw.hex()}]")
            else:
                print(decoded)
            print()
    else:
        print(decode_raw(args.file))


if __name__ == "__main__":
    main()
