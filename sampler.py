#!/usr/bin/env python3
"""Resource sampler for the gameplay benchmark. Run it on the Docker host that runs the servers.

usage: sampler.py <servers.json> <out_dir> <hours>
Every 10 s, per server (from its container's cgroup v2 and /proc):
  samples.csv   CPU %, memory (total, anon, file cache), cumulative disk read/write, cumulative network
                rx/tx, thread count, threads above 5 % CPU, busiest thread's CPU %
  cores.csv     CPU % attributed to the cores each thread last ran on
  threads.csv   CPU % per thread group (thread name with digits stripped, e.g. "tokio-rt-worker")
Every 30 s: `tick query` on every server's console (report.py reads the answers from docker logs).
Every 60 s: world size (if the server has "world_dir") and free space, with a disk guard that sends
`stop` to a server whose world passes MAX_WORLD_MIB, or to every server under MIN_FREE_GIB free.
"""
import collections, csv, os, re, shutil, subprocess, sys, time

import console

CFG = console.load_servers(sys.argv[1])
SERVERS = {srv["key"]: srv for srv in CFG["servers"]}
MIN_FREE_GIB = 20
MAX_WORLD_MIB = 5120
CLK = os.sysconf("SC_CLK_TCK")
INTERVAL = 10

out, hours = sys.argv[2], float(sys.argv[3])
os.makedirs(out, exist_ok=True)
start = time.time()
end = start + hours * 3600
events = open(os.path.join(out, "events.log"), "a", buffering=1)


def log(msg):
    events.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def cmd(srv, line):
    console.send(srv["console"], line)


def cgroup(container):
    try:
        cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", container], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except subprocess.TimeoutExpired:
        return None
    p = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
    return p if cid and os.path.isdir(p) else None


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def cg_stats(p):
    cpu = int(next(l for l in read(f"{p}/cpu.stat").splitlines() if l.startswith("usage_usec")).split()[1])
    mem = int(read(f"{p}/memory.current") or 0)
    ms = dict(l.split() for l in read(f"{p}/memory.stat").splitlines() if l)
    # io.stat repeats the same I/O for each block layer (NVMe and the device-mapper volumes on top),
    # so take the largest layer rather than the sum.
    rb = wb = 0
    for line in read(f"{p}/io.stat").splitlines():
        f = dict(x.partition("=")[::2] for x in line.split()[1:])
        rb, wb = max(rb, int(f.get("rbytes", 0))), max(wb, int(f.get("wbytes", 0)))
    procs = read(f"{p}/cgroup.procs").split()
    rx = tx = 0
    if procs:
        for line in read(f"/proc/{procs[0]}/net/dev").splitlines()[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == "lo":
                continue
            f = rest.split()
            rx += int(f[0]); tx += int(f[8])
    return cpu, mem, int(ms.get("anon", 0)), int(ms.get("file", 0)), rb, wb, rx, tx


def threads(p):
    """{tid: (group, cpu_ticks, processor)} for every thread in the cgroup."""
    # /proc/<tid>/stat reports the whole process; the per-thread numbers are under /proc/<pid>/task/<tid>.
    res = {}
    tasks = []
    for pid in read(f"{p}/cgroup.procs").split():
        try:
            tasks += [(pid, tid) for tid in os.listdir(f"/proc/{pid}/task")]
        except OSError:
            pass
    for pid, tid in tasks:
        s = read(f"/proc/{pid}/task/{tid}/stat")
        if not s:
            continue
        lp, rp = s.index("("), s.rindex(")")
        comm = s[lp + 1:rp]
        f = s[rp + 2:].split()
        # after "pid (comm) ": f[0]=state ... utime=f[11], stime=f[12], processor=f[36]
        res[tid] = (re.sub(r"[-_ ]?\d+$", "", comm) or comm, int(f[11]) + int(f[12]), int(f[36]))
    return res


def cpuset(p):
    s = read(f"{p}/cpuset.cpus.effective").strip() or read(f"{p}/cpuset.cpus").strip()
    cpus = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-"); cpus += range(int(a), int(b) + 1)
        elif part:
            cpus.append(int(part))
    return cpus


def world_mib(srv):
    if not srv.get("world_dir"):
        return 0
    r = subprocess.run(["du", "-sm", srv["world_dir"]], capture_output=True, text=True)
    return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout else 0


def writer(name, header):
    # Append, so a restarted sampler continues the same files.
    path = os.path.join(out, name)
    fresh = not os.path.exists(path)
    w = csv.writer(open(path, "a", newline="", buffering=1))
    if fresh:
        w.writerow(header)
    return w


samples = writer("samples.csv", ["unix_time", "server", "alive", "cpu_pct", "mem_mib", "anon_mib", "file_mib",
                                 "read_mib", "write_mib", "net_rx_mib", "net_tx_mib", "threads", "threads_active",
                                 "top_thread_pct"])
cores = writer("cores.csv", ["unix_time", "server", "cpu", "pct"])
groups = writer("threads.csv", ["unix_time", "server", "group", "pct", "threads"])
worlds = writer("world_mib.csv", ["unix_time", "server", "world_mib", "node_free_gib"])

last_cpu, last_thr, stopped = {}, {}, set()
log(f"start, {hours} h")
next_tick, next_min = start, start
while time.time() < end and len(stopped) < len(SERVERS):
    now = time.time()
    for name, srv in SERVERS.items():
        p = cgroup(srv["container"])
        if p is None:
            samples.writerow([int(now), name, 0] + [""] * 11)
            last_cpu.pop(name, None); last_thr.pop(name, None)
            continue
        cpu, mem, anon, filec, rb, wb, rx, tx = cg_stats(p)
        prev = last_cpu.get(name)
        pct = "" if prev is None else round(100 * (cpu - prev[1]) / 1e6 / (now - prev[0]), 1)
        last_cpu[name] = (now, cpu)
        thr = threads(p)
        pt = last_thr.get(name)
        active, top = 0, 0.0
        if pt is not None:
            dt = now - pt[0]
            per_core = collections.Counter({c: 0.0 for c in cpuset(p)})
            per_group, group_n = collections.Counter(), collections.Counter()
            for tid, (group, ticks, proc) in thr.items():
                old = pt[1].get(tid)
                # A thread born since the last sample counts from zero.
                d = 100 * (ticks - (old[1] if old else 0)) / CLK / dt
                if d <= 0:
                    group_n[group] += 1
                    continue
                per_core[proc] += d
                per_group[group] += d
                group_n[group] += 1
                active += d > 5
                top = max(top, d)
            for c, v in sorted(per_core.items()):
                cores.writerow([int(now), name, c, round(v, 1)])
            for g, v in per_group.most_common(12):
                groups.writerow([int(now), name, g, round(v, 1), group_n[g]])
        last_thr[name] = (now, thr)
        samples.writerow([int(now), name, 1, pct, round(mem / 2**20, 1), round(anon / 2**20, 1),
                          round(filec / 2**20, 1), round(rb / 2**20, 1), round(wb / 2**20, 1),
                          round(rx / 2**20, 2), round(tx / 2**20, 2), len(thr), active, round(top, 1)])
    if now >= next_tick:
        for name, srv in SERVERS.items():
            if name not in stopped:
                cmd(srv, "tick query")
        next_tick += 30
    if now >= next_min:
        free = shutil.disk_usage(CFG.get("disk_guard_path", "/")).free / 2**30
        for name, srv in SERVERS.items():
            size = world_mib(srv)
            worlds.writerow([int(now), name, size, round(free, 1)])
            if name not in stopped and size > MAX_WORLD_MIB:
                stopped.add(name); log(f"STOPPING {name}: world {size} MiB"); cmd(srv, "stop")
        if free < MIN_FREE_GIB:
            for name, srv in SERVERS.items():
                if name not in stopped:
                    stopped.add(name); log(f"STOPPING {name}: node {free:.1f} GiB free"); cmd(srv, "stop")
        next_min += 60
    time.sleep(max(0.0, INTERVAL - (time.time() - now)))
log("done")
