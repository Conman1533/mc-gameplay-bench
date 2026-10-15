#!/usr/bin/env python3
"""Writes a whitelist.json for one server's 8 bots: whitelist.py <bot_prefix> <vanilla|pumpkin> > whitelist.json

Offline-mode servers whitelist by UUID, and the offline UUID depends on the server: vanilla, Paper and
other Java servers use a version-3 UUID of "OfflinePlayer:<name>"; Pumpkin and its forks use the first
16 bytes of SHA-256(<name>).
"""
import hashlib, json, sys, uuid

from fleet import SLOTS


def offline_uuid(name, kind):
    if kind == "pumpkin":
        return str(uuid.UUID(bytes=hashlib.sha256(name.encode()).digest()[:16]))
    b = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode()).digest())
    b[6] = b[6] & 0x0F | 0x30
    b[8] = b[8] & 0x3F | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


prefix, kind = sys.argv[1], sys.argv[2]
json.dump([{"uuid": offline_uuid(prefix + s[0], kind), "name": prefix + s[0]} for s in SLOTS], sys.stdout, indent=2)
print()
