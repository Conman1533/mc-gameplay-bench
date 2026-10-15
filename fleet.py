#!/usr/bin/env python3
"""Realistic-session bot fleet: 8 bots per server, the same seeded schedule on every server.

usage: fleet.py <servers.json> <out_dir> <hours> <host>
Slots per server: 3 builders, 2 miners, 2 AFK, 1 explorer. Each slot plays sessions of 20-60 min with
5-15 min breaks; the schedule is seeded per slot, so every server sees the same one. At each join the
driver sets the bot's gamemode, gives it blocks and a pickaxe, and teleports it to its station through
the server's console (see console.py). Bot logs and fleet-events.log go to out_dir.
"""
import os, random, re, subprocess, sys, threading, time

import console

HERE = os.path.dirname(os.path.abspath(__file__))
SLOTS = [
    ("Builder0", "builder", (-20.5, 151, -19.5)),
    ("Builder1", "builder", (0.5, 151, -19.5)),
    ("Builder2", "builder", (20.5, 151, -19.5)),
    ("Miner0", "miner", (50.5, 131, 0.5)),
    ("Miner1", "miner", (50.5, 131, -159.5)),
    ("Afk0", "afk", (34.5, 151, -27.5)),
    ("Afk1", "afk", (0.5, 151, 31.5)),
    ("Explorer0", "explorer", (0.5, 151, 0.5)),
]
# Each miner tunnels inside its own stone block; it is refilled when the miner reaches its far end.
MINE_BOX = {"Miner0": (0, 146, "fill 50 130 0 90 133 149 stone"), "Miner1": (-160, -14, "fill 50 130 -160 90 133 -11 stone")}


def main():
    cfg, out, hours, host = console.load_servers(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4]
    end = time.time() + hours * 3600
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "start_unix"), "w") as f:
        f.write(f"{int(time.time())}\n")
    events = open(os.path.join(out, "fleet-events.log"), "a", buffering=1)
    lock = threading.Lock()

    def log(msg):
        with lock:
            events.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

    def run_slot(server, idx, slot, role, station):
        name, cons = f"{server['bot_prefix']}{slot}", server["console"]
        rng = random.Random(idx * 7919 + 17)
        mine_z = None
        time.sleep(idx * 15 + rng.uniform(0, 240))
        session = 0
        while time.time() < end - 120:
            session += 1
            length = min(rng.uniform(20, 60) * 60, end - time.time() - 30)
            st = list(station)
            if role == "miner":
                lo, hi, refill = MINE_BOX[slot]
                if mine_z is None or mine_z + 3 > hi:
                    if mine_z is not None:
                        console.send(cons, refill)
                    mine_z = lo
                else:
                    mine_z += 3
                st[2] = mine_z + 0.5
                console.send(cons, f"fill 50 131 {mine_z} 50 132 {mine_z} air")
            logf = os.path.join(out, f"bot-{name}.log")
            with open(logf, "a") as f:
                proc = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "bot", "survbot.py"), host,
                                         str(server["port"]), name, str(length), role, *map(str, st)],
                                        stdout=f, stderr=subprocess.STDOUT)
            joined = False
            for _ in range(60):
                time.sleep(1)
                if proc.poll() is not None:
                    break
                if open(logf).read().count(" joined") >= session:
                    joined = True
                    break
            if not joined:
                # Usually a connection throttle (Paper allows one join per IP every 4 s): retry soon.
                log(f"{name} session {session} failed to join; retrying")
                proc.kill(); proc.wait()
                session -= 1
                if role == "miner":
                    mine_z = None if mine_z is None or mine_z <= MINE_BOX[slot][0] else mine_z - 3
                time.sleep(rng.uniform(5, 20))
                continue
            gm = "creative" if role == "explorer" else "survival"
            for c in (f"gamemode {gm} {name}", f"clear {name}", f"give {name} cobblestone 64", f"give {name} diamond_pickaxe",
                      f"effect give {name} resistance 100000 4 true", f"effect give {name} saturation 100000 0 true",
                      f"tp {name} {st[0]} {st[1]} {st[2]}"):
                console.send(cons, c)
            log(f"{name} session {session} joined for {length / 60:.0f} min at {st}")
            t0 = time.time()
            while proc.poll() is None:
                time.sleep(5)
                since = time.time() - t0
                if role == "builder" and int(since) % 300 < 5 and since > 60:
                    console.send(cons, f"give {name} cobblestone 64")
                if role == "miner" and int(since) % 600 < 5 and since > 60:
                    console.send(cons, f"clear {name} cobblestone")
            if role == "miner":
                m = re.findall(r"pos=[-\d.]+,[-\d.]+,([-\d.]+)", open(logf).read())
                if m:
                    mine_z = int(float(m[-1]))
            if time.time() < t0 + length - 15:
                log(f"{name} session {session} ended early (exit {proc.returncode})")
            if time.time() < end - 120:
                time.sleep(rng.uniform(5, 15) * 60)

    threads = [threading.Thread(target=run_slot, args=(s, i, *slot), daemon=True)
               for s in cfg["servers"] for i, slot in enumerate(SLOTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log("fleet done")


if __name__ == "__main__":
    main()
