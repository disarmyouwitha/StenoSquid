"""Offline immutable index builder, not a firmware compactor or RAM estimate."""

import struct

ENTRY = struct.Struct("<QI")
MAX_ENTRIES = 1 << 20
MAX_COLLISIONS = 16


def fingerprint(key):
    """FNV-1a64 over an explicitly encoded key; verify full keys after probing."""
    result = 14695981039346656037
    for byte in key:
        result = ((result ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return result


def build(entries):
    entries = sorted(entries)
    if len(entries) > MAX_ENTRIES:
        raise ValueError("Index capacity")
    previous = None
    collisions = 0
    for key, address in entries:
        collisions = collisions + 1 if key == previous else 1
        previous = key
        if collisions > MAX_COLLISIONS:
            raise ValueError("Hash collision group exceeds bound")
        if not 0 <= address <= 0xFFFFFFFF:
            raise ValueError("Record address")
    return b"".join(ENTRY.pack(*entry) for entry in entries)
