#!/usr/bin/env python3
"""Role-playing offline bot for protocol 777 (Minecraft 26.3).

usage: survbot.py <host> <port> <name> <seconds> <role> <x> <y> <z>
roles:
  builder   stands at x,y,z; places a 3x3 patch of blocks (hotbar slot 0) on the floor in front,
            mines it back (pickaxe in slot 1) and opens the chest at x-2,y,z every 30 s
  miner     stands in a solid stone box at x,y,z (floor at y-1) and digs a 2-high serpentine
            tunnel: 40 blocks along +x/-x, then 3 blocks along +z
  afk       stands still at x,y,z, sending the idle updates a real client sends
  explorer  creative; walks slowly in a random direction from wherever it spawns
The driver teleports the bot to x,y,z and sets its gamemode and items; the bot waits for that.
Prints one STATS line every 60 s: confirmed placements, breaks, chest opens, item pickups.
"""
import math, os, queue, random, struct, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import walkbot as wb  # noqa: E402
from walkbot import varint, read_varint, string

SB_CHAT_COMMAND = 7
SB_CLIENT_COMMAND, SB_CONTAINER_CLOSE = 12, 19
SB_MOVE_POS_ROT, SB_MOVE_STATUS = 31, 33
SB_PLAYER_ACTION, SB_SET_CARRIED_ITEM, SB_USE_ITEM_ON = 41, 54, 66
CB_BLOCK_UPDATE, CB_LOGIN, CB_OPEN_SCREEN, CB_SET_HEALTH, CB_TAKE_ITEM = 8, 50, 60, 106, 127
START_DESTROY, STOP_DESTROY, DROP_ITEM = 0, 3, 5   # ServerboundPlayerActionPacket.Action ordinals in 26.3
FACE_UP = 1
WALK = 0.18                                   # blocks per tick
DIG_TICKS = 12                                # enough for stone or cobblestone with a diamond pickaxe
STAND_TOLERANCE = 1.5


def block_pos(x, y, z):
    return struct.pack(">Q", ((x & 0x3FFFFFF) << 38) | ((z & 0x3FFFFFF) << 12) | (y & 0xFFF))


def unpack_pos(v):
    x, z, y = v >> 38, (v >> 12) & 0x3FFFFFF, v & 0xFFF
    x -= (1 << 26) if x >= (1 << 25) else 0
    z -= (1 << 26) if z >= (1 << 25) else 0
    y -= (1 << 12) if y >= (1 << 11) else 0
    return x, y, z


class Bot:
    def __init__(self, c, role, station):
        self.c, self.role, self.station = c, role, station
        self.pos, self.eid, self.seq = None, None, 0
        self.stats = {"placed": 0, "broken": 0, "chests": 0, "pickups": 0, "deaths": 0}
        self.pending = {}                      # block pos -> "placed" | "broken" awaiting a block_update
        self.plan = []                         # queued (ticks_to_wait, action) steps
        self.wait = 0
        self.container = None
        self.heading = random.uniform(0, 2 * math.pi)
        self.dirx, self.leg = 1, 0             # miner state

    # --- packets -------------------------------------------------------------------------
    def next_seq(self):
        self.seq += 1
        return varint(self.seq)

    def look_at(self, tx, ty, tz):
        x, y, z = self.pos
        dx, dy, dz = tx - x, ty - (y + 1.62), tz - z
        yaw = -math.degrees(math.atan2(dx, dz))
        pitch = -math.degrees(math.atan2(dy, math.hypot(dx, dz)))
        self.c.send(SB_MOVE_POS_ROT, struct.pack(">dddff", x, y, z, yaw, pitch) + b"\x01")

    def move(self, nx, nz):
        self.pos[0], self.pos[2] = nx, nz
        self.c.send(wb.SB_PLAY_MOVE_POS, struct.pack(">ddd", *self.pos) + b"\x01")

    def hold(self, slot):
        self.c.send(SB_SET_CARRIED_ITEM, struct.pack(">h", slot))

    def place_on(self, bx, by, bz):
        """Right-click the top face of block bx,by,bz with the held item."""
        self.look_at(bx + 0.5, by + 1, bz + 0.5)
        self.pending[(bx, by + 1, bz)] = "placed"
        self.c.send(SB_USE_ITEM_ON, varint(0) + block_pos(bx, by, bz) + varint(FACE_UP)
                    + struct.pack(">fff", 0.5, 1.0, 0.5) + b"\x00\x00" + self.next_seq())

    def dig(self, bx, by, bz, phase):
        if phase == START_DESTROY:
            self.look_at(bx + 0.5, by + 0.5, bz + 0.5)
            self.pending[(bx, by, bz)] = "broken"
        self.c.send(SB_PLAYER_ACTION, varint(phase) + block_pos(bx, by, bz) + bytes([FACE_UP]) + self.next_seq())

    def open_chest(self, bx, by, bz):
        self.look_at(bx + 0.5, by + 0.5, bz + 0.5)
        self.c.send(SB_USE_ITEM_ON, varint(0) + block_pos(bx, by, bz) + varint(FACE_UP)
                    + struct.pack(">fff", 0.5, 1.0, 0.5) + b"\x00\x00" + self.next_seq())

    def close_container(self):
        if self.container is not None:
            self.c.send(SB_CONTAINER_CLOSE, varint(self.container))
            self.container = None

    def on_packet(self, pid, buf):
        if pid == CB_LOGIN:
            self.eid = struct.unpack(">i", buf.read(4))[0]
        elif pid == CB_BLOCK_UPDATE:
            p = unpack_pos(struct.unpack(">q", buf.read(8))[0])
            state = read_varint(buf)
            kind = self.pending.pop(p, None)
            if kind == "broken" and state == 0 or kind == "placed" and state != 0:
                self.stats[kind] += 1
        elif pid == CB_OPEN_SCREEN:
            self.container = read_varint(buf)
            self.stats["chests"] += 1
        elif pid == CB_TAKE_ITEM:
            read_varint(buf)
            if read_varint(buf) == self.eid:
                self.stats["pickups"] += 1
        elif pid == CB_SET_HEALTH:
            if struct.unpack(">f", buf.read(4))[0] <= 0:
                self.stats["deaths"] += 1
                self.c.send(SB_CLIENT_COMMAND, varint(0))   # perform respawn

    # --- behaviour -----------------------------------------------------------------------
    def at_station(self):
        sx, sy, sz = self.station
        return self.pos and abs(self.pos[0] - sx) < STAND_TOLERANCE and abs(self.pos[1] - sy) < STAND_TOLERANCE \
            and abs(self.pos[2] - sz) < STAND_TOLERANCE

    def builder_cycle(self):
        sx, sy, sz = (int(math.floor(v)) for v in self.station)
        cells = [(sx + dx, sz + dz) for dz in (2, 3, 4) for dx in (-1, 0, 1)]
        steps = [(5, lambda: self.hold(0))]
        for bx, bz in cells:
            steps.append((20, lambda bx=bx, bz=bz: self.place_on(bx, sy - 1, bz)))
        steps.append((10, lambda: self.hold(1)))
        for bx, bz in cells:
            steps.append((5, lambda bx=bx, bz=bz: self.dig(bx, sy, bz, START_DESTROY)))
            steps.append((DIG_TICKS, lambda bx=bx, bz=bz: self.dig(bx, sy, bz, STOP_DESTROY)))
        if random.random() < 0.6:
            steps.append((20, lambda: self.open_chest(sx - 2, sy, sz)))
            steps.append((60, self.close_container))
        steps.append((40, lambda: None))
        return steps

    def miner_cycle(self):
        x, y, z = (int(math.floor(v)) for v in self.pos)
        if self.leg < 40:
            dx, dz = self.dirx, 0
        else:
            dx, dz = 0, 1
        tx, tz = x + dx, z + dz
        steps = [(5, lambda: self.hold(1))]
        for ty in (y, y + 1):
            steps.append((4, lambda ty=ty: self.dig(tx, ty, tz, START_DESTROY)))
            steps.append((DIG_TICKS, lambda ty=ty: self.dig(tx, ty, tz, STOP_DESTROY)))
        for i in range(1, 6):
            steps.append((1, lambda i=i: self.move(x + 0.5 + dx * i / 5, z + 0.5 + dz * i / 5)))
        self.leg += 1
        if self.leg >= 43:
            self.leg, self.dirx = 0, -self.dirx
        steps.append((random.randint(5, 20), lambda: None))
        return steps

    def tick(self):
        if self.pos is None:
            return
        if self.role == "explorer":
            if random.random() < 0.005:
                self.heading = random.uniform(0, 2 * math.pi)
            self.pos[0] += math.cos(self.heading) * WALK * 0.5
            self.pos[2] += math.sin(self.heading) * WALK * 0.5
            self.c.send(wb.SB_PLAY_MOVE_POS, struct.pack(">ddd", *self.pos) + b"\x00")
            return
        if self.role == "dropper":
            # Test role: looks straight down and drops one held item every 2 s.
            if self.at_station() and self.wait <= 0:
                x, y, z = self.pos
                self.c.send(SB_MOVE_POS_ROT, struct.pack(">dddff", x, y, z, 0.0, 90.0) + b"\x01")
                self.c.send(SB_PLAYER_ACTION, varint(DROP_ITEM) + block_pos(0, 0, 0) + bytes([0]) + self.next_seq())
                self.stats["placed"] += 1
                self.wait = 40
            self.wait -= 1
            return
        if self.role == "afk" or (not self.plan and not self.at_station() and self.role == "builder"):
            if random.random() < 0.05:
                self.c.send(SB_MOVE_STATUS, b"\x01")
            return
        if self.wait > 0:
            self.wait -= 1
            return
        if not self.plan:
            if self.role == "builder":
                self.plan = self.builder_cycle()
            elif self.role == "miner" and (self.leg or self.at_station() or self.started):
                self.started = True
                self.plan = self.miner_cycle()
            else:
                return
        delay, action = self.plan.pop(0)
        action()
        self.wait = delay

    started = False


def main():
    host, port, name, seconds, role = sys.argv[1], int(sys.argv[2]), sys.argv[3], float(sys.argv[4]), sys.argv[5]
    station = [float(v) for v in sys.argv[6:9]] if len(sys.argv) >= 9 else [0, 0, 0]
    c = wb.Conn(host, port)
    wb.login(c, host, port, name)
    c.send(wb.SB_CFG_CLIENT_INFO, string("en_us") + bytes([8]) + varint(0) + b"\x01" + bytes([0x7F]) + varint(1) + b"\x00\x01" + varint(0))
    bot = Bot(c, role, station)
    inbox = queue.Queue()

    def reader():
        try:
            while True:
                inbox.put(c.recv())
        except Exception as e:  # noqa: BLE001
            inbox.put(("closed", str(e)))

    threading.Thread(target=reader, daemon=True).start()
    phase, deadline = "config", time.time() + seconds
    next_tick, next_stats, loaded = time.time(), time.time() + 60, False
    while time.time() < deadline:
        try:
            pid, buf = inbox.get(timeout=max(0.0, next_tick - time.time()))
        except queue.Empty:
            pid = None
        if pid == "closed":
            raise RuntimeError(f"connection closed: {buf}")
        if pid is not None and phase == "config":
            if pid == wb.CB_CFG_KNOWN_PACKS:
                c.send(wb.SB_CFG_KNOWN_PACKS, wb._known_packs(buf))
            elif pid == wb.CB_CFG_KEEP_ALIVE:
                c.send(wb.SB_CFG_KEEP_ALIVE, buf.read(8))
            elif pid == wb.CB_CFG_PING:
                c.send(wb.SB_CFG_PONG, buf.read(4))
            elif pid == wb.CB_CFG_COC:
                c.send(wb.SB_CFG_ACCEPT_COC)
            elif pid == wb.CB_CFG_FINISH:
                c.send(wb.SB_CFG_FINISH)
                phase = "play"
                print(f"{time.time():.0f} joined", flush=True)
            elif pid == wb.CB_CFG_DISCONNECT_ID:
                raise RuntimeError(f"config disconnect: {buf.read()[:200]!r}")
        elif pid is not None:
            if pid == wb.CB_PLAY_KEEP_ALIVE:
                c.send(wb.SB_PLAY_KEEP_ALIVE, buf.read(8))
            elif pid == wb.CB_PLAY_PLAYER_POSITION:
                tp_id = read_varint(buf)
                raw = buf.read(24)
                bot.pos = list(struct.unpack(">ddd", raw))
                buf.read(24)
                rot = buf.read(8)
                c.send(wb.SB_PLAY_ACCEPT_TP, varint(tp_id) + raw + rot)
                bot.plan, bot.wait = [], 0
                if not loaded:
                    c.send(wb.SB_PLAY_LOADED)
                    loaded = True
            elif pid == wb.CB_PLAY_CHUNK_BATCH_FINISHED:
                c.send(wb.SB_PLAY_CHUNK_BATCH, struct.pack(">f", 25.0))
            elif pid == wb.CB_PLAY_START_CFG:
                c.send(wb.SB_PLAY_CFG_ACK)
                phase = "config"
            elif pid == wb.CB_PLAY_DISCONNECT_ID:
                raise RuntimeError(f"play disconnect: {buf.read()[:300]!r}")
            else:
                try:
                    bot.on_packet(pid, buf)
                except (struct.error, EOFError):
                    pass
        if time.time() >= next_tick:
            next_tick = max(next_tick + 0.05, time.time() - 0.5)
            if phase == "play":
                bot.tick()
                c.send(wb.SB_PLAY_TICK_END)
        if time.time() >= next_stats:
            next_stats += 60
            print(f"{time.time():.0f} STATS " + " ".join(f"{k}={v}" for k, v in bot.stats.items()) + (" pos=%.1f,%.1f,%.1f" % tuple(bot.pos) if bot.pos else ""), flush=True)
    print(f"{time.time():.0f} STATS " + " ".join(f"{k}={v}" for k, v in bot.stats.items()) + (" pos=%.1f,%.1f,%.1f" % tuple(bot.pos) if bot.pos else ""), flush=True)
    print(f"{time.time():.0f} done", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"{time.time():.0f} ERROR {e}", flush=True)
        sys.exit(1)
