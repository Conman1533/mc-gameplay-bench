"""Sends console commands to a Minecraft server. Shared by fleet.py and sampler.py.

A server's "console" in servers.json is one of:
  wings:<server-uuid>   Pelican/Pterodactyl Wings API: POST $WINGS_URL/api/servers/<uuid>/commands with
                        "Authorization: Bearer $WINGS_TOKEN" (the node token from Wings' config.yml)
  file:<path>           append the command to a file the server reads its console from, e.g.
                        `tail -n0 -f cmds.txt | ./server`
  exec:<command>        run a shell command; {cmd} is replaced with the shell-quoted console command
"""
import json, os, shlex, subprocess, threading, urllib.request

_lock = threading.Lock()


def send(console, cmd):
    kind, _, target = console.partition(":")
    if kind == "file":
        with _lock, open(target, "a") as f:
            f.write(cmd + "\n")
    elif kind == "wings":
        req = urllib.request.Request(
            f"{os.environ['WINGS_URL'].rstrip('/')}/api/servers/{target}/commands",
            data=json.dumps({"commands": [cmd]}).encode(), method="POST",
            headers={"Authorization": f"Bearer {os.environ['WINGS_TOKEN']}", "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except OSError:
            pass
    elif kind == "exec":
        subprocess.run(target.replace("{cmd}", shlex.quote(cmd)), shell=True, capture_output=True, timeout=30)
    else:
        raise ValueError(f"unknown console {console!r}")


def load_servers(path):
    with open(path) as f:
        return json.load(f)
