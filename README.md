# Minecraft gameplay benchmark

A head-to-head benchmark for Minecraft Java servers (vanilla, Paper, Pumpkin, PEX and anything else that
speaks the protocol) under a realistic, repeatable play session instead of a synthetic load.

Every server gets its own copy of the same world and the same 8 bot players, who join and leave on the same
seeded schedule:

| Bots | What they do |
|---|---|
| 3 builders | place a 3×3 patch of cobblestone, mine it back, and open a chest every 30 s or so |
| 2 miners | dig a 2-high tunnel through a stone block, 40 blocks one way, 3 across, then back |
| 2 AFK | stand still, sending the idle updates a real client sends |
| 1 explorer | creative mode, walks slowly in a random direction, loading new terrain |

Each bot plays 20–60 minute sessions with 5–15 minute breaks. The world (`world/base-world-262.tgz`, built on
vanilla 26.2) also has an egg farm (chickens → hoppers → chest) and a 9×9 wheat farm that run on their own.

While the bots play, the sampler records every 10 s, per server:

- CPU, and how it is spread over threads: cores busy, the busiest thread's share, threads above 5 % of a core,
  CPU by thread name (main thread, chunk workers, GC, network…)
- memory (anonymous vs page cache), disk read and written, network received and sent, world size
- tick time (`tick query` every 30 s: average, P50/P95/P99)

and the bots record what the server confirmed: blocks placed and broken, chests opened, items picked up.
`report.py` turns it all into one HTML page with a scorecard and charts.

## Requirements

- Python 3.10+ on the machine that runs the bots (no packages needed)
- Servers running in Docker on Linux with cgroup v2, and the sampler running on that Docker host
  (it reads each container's cgroup and `/proc`)
- Minecraft 26.3 (protocol 777); the bots log in in offline mode, so each server needs `online-mode=false`
  and, unless it is on a private network, a whitelist of the bot names (`whitelist.py`)

## Running it

Per-server setup for vanilla, Paper, Pumpkin, PEX, GraalVM builds, SteelMC and others is in
[SERVERS.md](SERVERS.md).

1. **Give every server a fresh copy of the world.** Extract `world/base-world-262.tgz` and use
   `base-world-262/` as each server's `world/`. Use the same view distance (10) and simulation distance (10)
   everywhere, and turn off spawn protection: the builders work near spawn.
2. **Whitelist the bots** (offline UUIDs differ between Java servers and Pumpkin-based ones):
   ```
   python3 whitelist.py Paper vanilla > whitelist.json          # vanilla, Paper, Spigot…
   python3 whitelist.py Pex pumpkin > data/whitelist.json        # Pumpkin, PEX
   ```
3. **Describe the servers** in a `servers.json` (see `example/servers.json`). Per server: `key`, `label`,
   `port`, `bot_prefix` (bot names are `<prefix>Builder0`…), `container` (Docker name or ID), `console`, and
   optionally `world_dir` for the size chart and disk guard. The order sets the chart colours.
   `console` is how commands reach the server (see `console.py`):
   - `wings:<server-uuid>`, for Pelican or Pterodactyl; set `WINGS_URL` and `WINGS_TOKEN` (the node's token)
   - `file:<path>`, for a server started as `tail -n0 -f cmds.txt | ./server`
   - `exec:<command>`, any command, with `{cmd}` replaced by the console command
4. **Start the servers, then the sampler and the fleet** (the sampler on the Docker host):
   ```
   python3 sampler.py servers.json run/ 2.05
   python3 fleet.py servers.json bots/ 2 <server-host-ip>
   ```
5. **Build the report** at any point during or after the run:
   ```
   python3 report.py servers.json run/ bots/ report.html [findings.html]
   ```
   `findings.html` is an optional HTML fragment shown at the top. If Docker runs inside a container (for example
   a Proxmox LXC), set `CMD_PREFIX`, e.g. `CMD_PREFIX="pct exec 109 --"`, so the report can read `docker logs`.

The sampler's disk guard sends `stop` to a server whose world passes 5 GiB, or to all of them when the disk
under `disk_guard_path` has less than 20 GiB free.

## Things to know

- **Equal limits.** Give every server the same CPU and memory limits. For Java servers, match heap settings
  too (`-Xms`/`-Xmx` or `MaxRAMPercentage`). A heap fixed at its maximum looks worse on memory for reasons that
  have nothing to do with the server.
- **Profilers and telemetry.** Paper bundles spark (a sampling profiler) and bStats. Turn them off on every
  Paper-based server, or on none, so they're compared like for like.
- **The anti-fly check** on vanilla-style servers occasionally kicks a bot ("Flying is not enabled"). Set
  `allow-flight=true` to avoid it. The report counts dropped sessions per server, so check that row before
  comparing gameplay.
- **Paper's connection throttle** refuses a reconnect from the same IP within 4 s. The fleet retries failed
  joins after 5–20 s.
- **Vanilla-style servers won't run `fill`/`setblock` in unloaded chunks.** Anything that builds stations
  must run while players are near them (or use `forceload`).
- **Bot counts are server confirmations.** Vanilla-style servers confirm block breaks with a different packet
  than Pumpkin, so compare block-break counts only between servers of the same family.
- **Background load matters less than you'd think, but it isn't zero.** Run all competitors at the same time on
  the same host, so noise hits them equally.

## Results (2026-10-14, one Pelican node, 4 CPU cores and 8 GB per server)

**Vanilla 26.3 vs Paper 26.3 #186 vs Pumpkin (upstream 5b53f064a) vs PEX (bbc4cd07e)**, 8 bots each for 2 hours:

| | Vanilla | Paper | Pumpkin | PEX |
|---|---|---|---|---|
| Avg MSPT | 21.0 ms | 15.9 ms | 10.0 ms | 8.4 ms |
| Cores busy | 0.58 | 0.45 | 0.45 | 0.37 |
| Memory (avg) | 1,615 MiB | 1,833 MiB | 1,440 MiB | 611 MiB |
| Disk written | 1,195 MiB | 303 MiB | 13,352 MiB | 214 MiB |
| Network sent | 692 MiB | 642 MiB | 745 MiB | 959 MiB |

**Paper on the JVM vs the same build as a GraalVM native image vs PEX**, 8 bots each for 2 hours:

| | Paper (JVM) | GraalVM Paper | PEX |
|---|---|---|---|
| Avg MSPT | 13.7 ms | 16.5 ms | 7.1 ms |
| Cores busy | 0.42 | 0.46 | 0.37 |
| Memory (avg) | 1,786 MiB | 1,151 MiB | 507 MiB |
| Startup with this world | 42.4 s | 32.6 s | 0.5 s |
| Network sent | 614 MiB | 626 MiB | 923 MiB |

## Files

| File | |
|---|---|
| `bot/walkbot.py` | minimal offline 26.3 client: login, configuration, keep-alive, chunk batches |
| `bot/survbot.py` | the role-playing bot (builder, miner, AFK, explorer) on top of it |
| `fleet.py` | runs 8 bots per server on the seeded session schedule |
| `sampler.py` | per-server CPU, threads, memory, disk, network, tick time and world size |
| `report.py`, `template.html` | the report page |
| `console.py` | sends console commands (Wings API, a console file, or a command) |
| `whitelist.py` | offline-mode whitelist for the bots |
| `world/base-world-262.tgz` | the benchmark world |

## License

MIT. See [LICENSE](LICENSE).
