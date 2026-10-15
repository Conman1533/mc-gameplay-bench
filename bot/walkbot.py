#!/usr/bin/env python3
"""Minimal offline-mode Java 26.3 (protocol 777) client: logs in, stays alive, keeps chunks loaded.

usage: bot.py <host> <port> <name> <seconds>
"""
import io
import socket
import struct
import sys
import time
import uuid
import zlib

PROTOCOL = 777

# serverbound
SB_INTENTION = 0
SB_LOGIN_HELLO, SB_LOGIN_ACK = 0, 3
SB_CFG_CLIENT_INFO, SB_CFG_FINISH, SB_CFG_KEEP_ALIVE, SB_CFG_PONG, SB_CFG_KNOWN_PACKS, SB_CFG_ACCEPT_COC = 0, 3, 4, 5, 7, 9
SB_PLAY_ACCEPT_TP, SB_PLAY_CHUNK_BATCH, SB_PLAY_CFG_ACK, SB_PLAY_KEEP_ALIVE, SB_PLAY_LOADED = 0, 11, 16, 28, 44
# clientbound
CB_LOGIN_DISCONNECT, CB_LOGIN_FINISHED, CB_LOGIN_COMPRESSION = 0, 2, 3
CB_CFG_DISCONNECT, CB_CFG_FINISH, CB_CFG_KEEP_ALIVE, CB_CFG_PING, CB_CFG_KNOWN_PACKS, CB_CFG_COC = 2, 3, 4, 5, 15, 20
CB_PLAY_CHUNK_BATCH_FINISHED, CB_PLAY_DISCONNECT, CB_PLAY_KEEP_ALIVE, CB_PLAY_PLAYER_POSITION, CB_PLAY_START_CFG = 11, None, 45, 73, 120


def varint(n):
    n &= 0xFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def read_varint(stream):
    num = shift = 0
    while True:
        raw = stream.read(1)
        if not raw:
            raise EOFError
        b = raw[0]
        num |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
    if num & 0x80000000:
        num -= 1 << 32
    return num


def string(s):
    data = s.encode()
    return varint(len(data)) + data


def read_string(buf):
    return buf.read(read_varint(buf)).decode()


class Conn:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port))
        self.file = self.sock.makefile("rb")
        self.threshold = -1

    def send(self, pid, payload=b""):
        data = varint(pid) + payload
        if self.threshold >= 0:
            if len(data) >= self.threshold:
                data = varint(len(data)) + zlib.compress(data)
            else:
                data = varint(0) + data
        self.sock.sendall(varint(len(data)) + data)

    def recv(self):
        length = read_varint(self.file)
        data = self.file.read(length)
        buf = io.BytesIO(data)
        if self.threshold >= 0:
            data_len = read_varint(buf)
            if data_len:
                buf = io.BytesIO(zlib.decompress(buf.read()))
        return read_varint(buf), buf



SB_PLAY_TICK_END, SB_PLAY_MOVE_POS = 13, 30
CB_PLAY_DISCONNECT_ID, CB_CFG_DISCONNECT_ID = 32, 2
STEP = 0.2  # blocks per tick, roughly walking speed


def login(c, host, port, name):
    c.send(SB_INTENTION, varint(PROTOCOL) + string(host) + struct.pack(">H", port) + varint(2))
    c.send(SB_LOGIN_HELLO, string(name) + uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + name).bytes)
    while True:
        pid, buf = c.recv()
        if pid == CB_LOGIN_COMPRESSION:
            c.threshold = read_varint(buf)
        elif pid == CB_LOGIN_FINISHED:
            c.send(SB_LOGIN_ACK)
            return
        elif pid == CB_LOGIN_DISCONNECT:
            raise RuntimeError(f"login disconnect: {buf.read()[:200]!r}")


def main():
    import math, queue, random, threading
    host, port, name, seconds = sys.argv[1], int(sys.argv[2]), sys.argv[3], float(sys.argv[4])
    c = Conn(host, port)
    login(c, host, port, name)
    c.send(SB_CFG_CLIENT_INFO, string("en_us") + bytes([8]) + varint(0) + b"\x01" + bytes([0x7F]) + varint(1) + b"\x00\x01" + varint(0))
    inbox = queue.Queue()

    def reader():
        try:
            while True:
                inbox.put(c.recv())
        except Exception as e:  # noqa: BLE001
            inbox.put(("closed", str(e)))

    threading.Thread(target=reader, daemon=True).start()
    phase, deadline = "config", time.time() + seconds
    pos, heading, next_tick, loaded = None, random.uniform(0, 2 * math.pi), time.time(), False
    while time.time() < deadline:
        try:
            pid, buf = inbox.get(timeout=max(0.0, next_tick - time.time()))
        except queue.Empty:
            pid = None
        if pid == "closed":
            raise RuntimeError(f"connection closed: {buf}")
        if pid is not None and phase == "config":
            if pid == CB_CFG_KNOWN_PACKS:
                c.send(SB_CFG_KNOWN_PACKS, _known_packs(buf))
            elif pid == CB_CFG_KEEP_ALIVE:
                c.send(SB_CFG_KEEP_ALIVE, buf.read(8))
            elif pid == CB_CFG_PING:
                c.send(SB_CFG_PONG, buf.read(4))
            elif pid == CB_CFG_COC:
                c.send(SB_CFG_ACCEPT_COC)
            elif pid == CB_CFG_FINISH:
                c.send(SB_CFG_FINISH)
                phase = "play"
                print(f"{time.time():.0f} joined", flush=True)
            elif pid == CB_CFG_DISCONNECT_ID:
                raise RuntimeError(f"config disconnect: {buf.read()[:200]!r}")
        elif pid is not None:
            if pid == CB_PLAY_KEEP_ALIVE:
                c.send(SB_PLAY_KEEP_ALIVE, buf.read(8))
            elif pid == CB_PLAY_PLAYER_POSITION:
                tp_id = read_varint(buf)
                raw = buf.read(24)
                pos = list(struct.unpack(">ddd", raw))
                buf.read(24)
                rot = buf.read(8)
                c.send(SB_PLAY_ACCEPT_TP, varint(tp_id) + raw + rot)
                if not loaded:
                    c.send(SB_PLAY_LOADED)
                    loaded = True
            elif pid == CB_PLAY_CHUNK_BATCH_FINISHED:
                c.send(SB_PLAY_CHUNK_BATCH, struct.pack(">f", 25.0))
            elif pid == CB_PLAY_START_CFG:
                c.send(SB_PLAY_CFG_ACK)
                phase = "config"
            elif pid == CB_PLAY_DISCONNECT_ID:
                raise RuntimeError(f"play disconnect: {buf.read()[:300]!r}")
        if time.time() >= next_tick:
            next_tick = max(next_tick + 0.05, time.time() - 0.5)
            if phase == "play" and pos is not None:
                if random.random() < 0.01:
                    heading = random.uniform(0, 2 * math.pi)
                pos[0] += math.cos(heading) * STEP
                pos[2] += math.sin(heading) * STEP
                c.send(SB_PLAY_MOVE_POS, struct.pack(">ddd", *pos) + b"\x00")
                c.send(SB_PLAY_TICK_END)
    print(f"{time.time():.0f} done", flush=True)


def _known_packs(buf):
    count = read_varint(buf)
    packs = [(read_string(buf), read_string(buf), read_string(buf)) for _ in range(count)]
    return varint(len(packs)) + b"".join(string(a) + string(b) + string(v) for a, b, v in packs)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"{time.time():.0f} ERROR {e}", flush=True)
        sys.exit(1)
