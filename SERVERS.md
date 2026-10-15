# Setting up each server

The bots are plain offline-mode Minecraft 26.3 clients (protocol 777) with no encryption. Every server in a run
needs to accept them, start from the benchmark world, and take console commands. This page lists what that takes
for each server family. The settings that matter for a fair comparison are the same everywhere:

| Setting | Value |
|---|---|
| Online mode | off (with a whitelist of the bot names unless the server is on a private network) |
| View distance / simulation distance | 10 / 10 |
| Spawn protection | 0 (the builders work near spawn) |
| CPU and memory limits | identical for every server |
| World | a fresh copy of `world/base-world-262.tgz` for every server, every run |

## Vanilla, Paper, Purpur, Spigot and other Java servers

`server.properties`:

```properties
online-mode=false
white-list=true
spawn-protection=0
view-distance=10
simulation-distance=10
allow-flight=true
```

- `allow-flight=true` stops the anti-fly check from kicking bots that stand on block edges or stop mid-air while
  digging. Without it, expect a few "Flying is not enabled" drops.
- Whitelist: `python3 whitelist.py <bot_prefix> vanilla > whitelist.json`, then start the server or run
  `whitelist reload`.
- World: extract the tarball and rename `base-world-262/` to `world/` (or whatever `level-name` says). The server
  upgrades it to 26.3 on first start, which takes a while on the first run.
- Paper-based servers:
  - Set `spark.enabled: false` in `config/paper-global.yml` and `enabled: false` in `plugins/bStats/config.yml`
    on all of them, or on none, so they're compared like for like.
  - Paper refuses a second login from one IP within 4 s. The fleet retries, but you can also set
    `settings.connection-throttle: -1` in `bukkit.yml`.
- Java heap: give every Java server the same heap settings (`-Xms`/`-Xmx` or `-XX:MaxRAMPercentage`).
- Console:
  - Pelican/Pterodactyl: `wings:<server-uuid>`.
  - The itzg/minecraft-server Docker image: `exec:docker exec <container> rcon-cli {cmd}`.
  - A plain `java -jar` started as `tail -n0 -f cmds.txt | java -jar server.jar nogui`: `file:cmds.txt`.
- `tick query` is built in (26.x), so MSPT works without plugins.

## Pumpkin and PEX

`pumpkin.toml`:

```toml
spawn_protection = 0
white_list = true
enforce_whitelist = true

[networking.java]
encryption = false     # the bots can't do encryption; Pumpkin keeps this separate from online mode
online_mode = false
view_distance = 10
simulation_distance = 10
```

`online_mode` and `view_distance` / `simulation_distance` appear twice in `pumpkin.toml` (Java and Bedrock
sections). Set both.

- `encryption = false` is required. With it on, the bots hang at login and the server logs nothing.
- Whitelist: Pumpkin computes offline UUIDs differently from Java servers, so use
  `python3 whitelist.py <bot_prefix> pumpkin > data/whitelist.json`, then start the server or run `whitelist reload`.
- World: extract the tarball and rename `base-world-262/` to `world/`. Pumpkin and PEX read the vanilla world as is.
- Console:
  - Pelican: `wings:<server-uuid>`. Set `use_tty = false` in `pumpkin.toml`, or the panel's stop can hang.
  - Running it yourself: `tail -n0 -f cmds.txt | ./pumpkin` with `use_tty = false`, and `file:cmds.txt`.
- Storage settings are part of what you're measuring. Upstream Pumpkin defaults to LZ4 with whole-file region
  writes, PEX to zlib with in-place writes. Leave each on its own defaults, or set them the same on purpose, and
  say which in your notes (`[world.chunk]`: `write_in_place`, `compression.algorithm`).
- Pumpkin's telemetry heartbeat is on by default (`[telemetry] enabled`). It's negligible, but you may want it off.

## GraalVM native-image builds of Paper

These run like Paper, with three differences:

- They can't load plugins, Netty's epoll transport or Velocity's native compression, so they use the Java
  fallbacks. That's part of what you're measuring.
- Launch the binary with the same heap flags as the JVM build you compare against. A wrapper that fixes the heap
  at its maximum (`-Xms` = `-Xmx`) makes memory look worse.
- Command-line options that only run through reflection may be missing from the build. One native Paper build
  crashed on `--port`; set the port in `server.properties` instead.

## SteelMC

[SteelMC](https://github.com/Steel-Foundation/SteelMC) can't run this benchmark yet. As of October 2026:

- **Version:** it targets Minecraft 26.2, and these bots speak 26.3 (protocol 777). They need the 26.2 protocol
  number and packet IDs, in `bot/walkbot.py` and `bot/survbot.py`.
- **World:** it stores worlds in its own region format, not vanilla's Anvil, so it can't load
  `world/base-world-262.tgz`. You'd have to rebuild the work areas with commands on a SteelMC-generated world,
  and the egg and wheat farms need entity and block behaviour it doesn't have yet.
- **Commands:** it has `fill`, `setblock`, `gamemode`, `give`, `clear`, `teleport` and `tick query`, but no
  `effect`. The fleet gives bots resistance and saturation with `effect`; without it they can take damage
  and get hungry.

Once it targets the same version as the other servers and can load Anvil worlds, it should only need a
`servers.json` entry like the others, plus a whitelist for its UUID scheme.

## Other servers (Minestom, Folia, modded, proxies)

Anything that speaks the same protocol version can take part. Check four things:

1. The bots can log in offline, with no encryption.
2. The server loads a vanilla Anvil world, so the work areas and farms exist.
3. `gamemode`, `clear`, `give`, `effect`, `tp` and `fill` work from the console.
4. `tick query` answers in vanilla's format ("Average time per tick: … ms"). Otherwise the MSPT charts stay
   empty, and the rest of the report still works.

For modded servers (Fabric, NeoForge), keep the mod list identical across runs and name it in the notes. Don't
put a proxy (Velocity, BungeeCord) in front of only some of the servers.
