#!/usr/bin/env python3
"""Builds the benchmark report page: report.py <servers.json> <sampler_dir> <fleet_dir> <out.html> [findings.html]

Reads the sampler's CSVs, MSPT from each server's timestamped `tick query` answers (docker logs -t) and
bot activity from the bots' STATS lines, buckets everything into 1-minute steps from the fleet's start and
writes the dataset into template.html. Prints a summary table. If Docker runs somewhere else (for example
inside a Proxmox container), set CMD_PREFIX to the command that reaches it, e.g. CMD_PREFIX="pct exec 109 --".
"""
import collections, csv, glob, io, json, os, re, shlex, statistics, subprocess, sys, time
from datetime import datetime, timezone

cfg_path, run, bots_dir, out_html = sys.argv[1:5]
template = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
with open(cfg_path) as f:
    CFG = json.load(f)
SERVERS = {srv["key"]: (srv["label"], srv["container"], srv["bot_prefix"]) for srv in CFG["servers"]}
PREFIX = shlex.split(os.environ.get("CMD_PREFIX", ""))
start = int(open(os.path.join(bots_dir, "start_unix")).read())
now = int(time.time())


def ct(*args):
    r = subprocess.run([*PREFIX, *args], capture_output=True, text=True)
    return r.stdout + r.stderr


def rows(name):
    with open(os.path.join(run, name)) as f:
        return list(csv.DictReader(f))


def minute(t):
    return int((int(t) - start) // 60)


samples, threads, worlds = rows("samples.csv"), rows("threads.csv"), rows("world_mib.csv")
# Per-thread data before the sampler fix (thread_fix_unix) attributed whole-process CPU to every thread.
try:
    thread_fix = int(open(os.path.join(bots_dir, "thread_fix_unix")).read())
except OSError:
    thread_fix = 0
threads = [r for r in threads if int(r["unix_time"]) >= thread_fix]
THREAD_FIELDS = {"threads_active", "top_thread_pct"}
last_min = max(minute(r["unix_time"]) for r in samples)
mins = list(range(0, last_min + 1))


def series(values_by_min, agg=statistics.fmean, nd=1):
    return [round(agg(values_by_min[m]), nd) if values_by_min.get(m) else None for m in mins]


data = {"title": CFG.get("title", "Gameplay Benchmark"), "subtitle": CFG.get("subtitle", ""), "notes": CFG.get("notes", []), "start": start, "generated": now, "minutes": mins, "servers": [], "summary": {}, "threads": {}}
avg_re = re.compile(r"Average time per tick: ([\d.]+) ?ms")
pct_re = re.compile(r"P50: ([\d.]+) ?ms,? P95: ([\d.]+) ?ms,? P99: ([\d.]+) ?ms")
ts_re = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")

for key, (label, container, prefix) in SERVERS.items():
    data["servers"].append({"key": key, "label": label})
    srows = [r for r in samples if r["server"] == key and r["alive"] == "1"]
    by = collections.defaultdict(lambda: collections.defaultdict(list))
    cum = {}
    for r in srows:
        m = minute(r["unix_time"])
        thread_ok = int(r["unix_time"]) >= thread_fix
        for f in ("cpu_pct", "mem_mib", "anon_mib", "file_mib", "threads", "threads_active", "top_thread_pct"):
            if r[f] != "" and (thread_ok or f not in THREAD_FIELDS):
                by[f][m].append(float(r[f]))
        if thread_ok and r["cpu_pct"] and r["top_thread_pct"] and float(r["cpu_pct"]) > 0:
            by["top_share"][m].append(100 * float(r["top_thread_pct"]) / float(r["cpu_pct"]))
        for f in ("read_mib", "write_mib", "net_tx_mib", "net_rx_mib"):
            cum.setdefault(f, {})[m] = float(r[f])  # last cumulative value in each minute
    s = {f: series(by[f]) for f in by}
    s["cores"] = [round(v / 100, 2) if v is not None else None for v in s.get("cpu_pct", [])]
    for f, per in cum.items():   # per-minute deltas of cumulative counters (MiB per minute)
        out, prev = [], None
        for m in mins:
            v = per.get(m)
            out.append(round(v - prev, 2) if v is not None and prev is not None and v >= prev else None)
            prev = v if v is not None else prev
        s[f.replace("_mib", "_per_min")] = out
    # MSPT from tick query output
    since = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    logs = ct("docker", "logs", "-t", "--since", since, container)
    mspt, p95 = collections.defaultdict(list), collections.defaultdict(list)
    for line in logs.splitlines():
        tm = ts_re.match(line)
        if not tm:
            continue
        t = datetime.strptime(tm.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        if (a := avg_re.search(line)):
            mspt[minute(t)].append(float(a.group(1)))
        if (p := pct_re.search(line)):
            p95[minute(t)].append(float(p.group(2)))
    s["mspt"] = series(mspt, nd=2)
    s["mspt_p95"] = series(p95, nd=2)
    w = {minute(r["unix_time"]): float(r["world_mib"]) for r in worlds if r["server"] == key}
    s["world_mib"] = [w.get(m) for m in mins]
    # bot activity: STATS lines are cumulative per session, so sum positive deltas per minute
    acts = collections.defaultdict(lambda: collections.Counter())
    totals = collections.Counter()
    early = 0
    for f in glob.glob(os.path.join(bots_dir, f"bot-{prefix}*.log")):
        prev = {}
        for line in open(f, errors="replace"):
            parts = line.split()
            if len(parts) < 3 or parts[1] != "STATS":
                continue
            vals = dict(p.split("=", 1) for p in parts[2:] if "=" in p and not p.startswith("pos="))
            m = minute(parts[0])
            for k in ("placed", "broken", "chests", "pickups", "deaths"):
                v = int(vals.get(k, 0)); old = prev.get(k, 0)
                d = v - old if v >= old else v
                acts[k][m] += d; totals[k] += d; prev[k] = v
    try:
        early = sum(1 for l in open(os.path.join(bots_dir, "fleet-events.log")) if l.split()[1].startswith(prefix) and "ended early" in l)
    except OSError:
        pass
    s["actions"] = [acts["placed"][m] + acts["broken"][m] + acts["chests"][m] + acts["pickups"][m] if m < last_min else None for m in mins]
    data["series_" + key] = s
    # thread groups: mean CPU % per group over all samples in which the server was sampled
    trows = [r for r in threads if r["server"] == key]
    n_samples = len({r["unix_time"] for r in trows}) or 1
    g = collections.Counter()
    # Linux keeps only 15 characters of a thread name: the native image keeps the end, so restore the start.
    FIX = {"ommon Worker #": "Paper Common Worker", "Netty NIO IO #": "Netty NIO IO", "nagement Thread": "Management Thread",
           "Netty Epoll IO ": "Netty Epoll IO", "r I/O Worker #": "Paper I/O Worker"}
    rename = lambda n: FIX.get(n, n)
    gthreads = {}
    for r in trows:
        name = rename(r["group"])
        g[name] += float(r["pct"])
        gthreads[name] = max(gthreads.get(name, 0), int(r["threads"]))
    groups = [{"group": k, "pct": round(v / n_samples, 2), "threads": gthreads[k]} for k, v in g.most_common()]
    data["threads"][key] = groups
    cpu = [float(r["cpu_pct"]) for r in srows if r["cpu_pct"]]
    mem = [float(r["mem_mib"]) for r in srows]
    wr = [float(r["write_mib"]) for r in srows]
    rd = [float(r["read_mib"]) for r in srows]
    tx = [float(r["net_tx_mib"]) for r in srows]
    allm = [v for vs in mspt.values() for v in vs]
    allp = [v for vs in p95.values() for v in vs]
    fixed = [r for r in srows if int(r["unix_time"]) >= thread_fix]
    tops = [float(r["top_thread_pct"]) for r in fixed if r["top_thread_pct"]]
    act = [float(r["threads_active"]) for r in fixed if r["threads_active"]]
    ws = [v for v in s["world_mib"] if v is not None]
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(len(xs) * p))] if xs else None
    data["summary"][key] = {
        "cpu_avg": round(statistics.fmean(cpu), 1) if cpu else None, "cpu_p95": q(cpu, .95),
        "cores_avg": round(statistics.fmean(cpu) / 100, 2) if cpu else None,
        "top_thread_avg": round(statistics.fmean(tops), 1) if tops else None,
        "threads_active_avg": round(statistics.fmean(act), 1) if act else None,
        "threads_max": max((int(float(r["threads"])) for r in srows if r["threads"]), default=None),
        "mem_avg": round(statistics.fmean(mem)) if mem else None, "mem_peak": round(max(mem)) if mem else None,
        "written_mib": round(wr[-1] - wr[0], 1) if wr else None, "read_mib": round(rd[-1] - rd[0], 1) if rd else None,
        "net_tx_mib": round(tx[-1] - tx[0], 1) if tx else None, "world_mib": ws[-1] if ws else None,
        "mspt_avg": round(statistics.fmean(allm), 2) if allm else None, "mspt_p95": round(statistics.fmean(allp), 2) if allp else None,
        "mspt_max": max(allm) if allm else None, "over_50": sum(1 for v in allm if v > 50),
        "placed": totals["placed"], "broken": totals["broken"], "chests": totals["chests"],
        "pickups": totals["pickups"], "deaths": totals["deaths"], "ended_early": early,
    }

data["elapsed_min"] = round((max(int(r["unix_time"]) for r in samples) - start) / 60, 1)
data["thread_fix_min"] = round((thread_fix - start) / 60, 1) if thread_fix else 0
html = open(template).read().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
if len(sys.argv) > 5:   # optional hand-written findings (HTML fragment)
    html = html.replace("<!--__FINDINGS__-->", open(sys.argv[5]).read())
else:
    html = html.replace('<section class="findings" id="findings-box">', '<section class="findings" id="findings-box" hidden>')
open(out_html, "w").write(html)
hdr = ["server", "cpu%", "cores", "top thr%", "act thr", "mem avg", "mem pk", "written", "read", "net tx",
       "world", "mspt", "p95", "max", ">50", "placed", "broken", "chests", "pickups", "early"]
print(f"elapsed {data['elapsed_min']} min")
print(" ".join(f"{h:>8}" for h in hdr))
for key, (label, _, _) in SERVERS.items():
    sm = data["summary"][key]
    vals = [label, sm["cpu_avg"], sm["cores_avg"], sm["top_thread_avg"], sm["threads_active_avg"], sm["mem_avg"],
            sm["mem_peak"], sm["written_mib"], sm["read_mib"], sm["net_tx_mib"], sm["world_mib"], sm["mspt_avg"],
            sm["mspt_p95"], sm["mspt_max"], sm["over_50"], sm["placed"], sm["broken"], sm["chests"], sm["pickups"],
            sm["ended_early"]]
    print(" ".join(f"{str(v):>8}" for v in vals))
for key in SERVERS:
    print(key, "threads:", ", ".join(f"{g['group']} {g['pct']}%" for g in data["threads"][key][:6]))
