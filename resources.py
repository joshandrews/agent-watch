# Keeps the machine healthy while agents build: watches CPU, memory and disk, stops dev stacks nobody is using,
# and runs the safe disk cleanup when space gets low. Called by watcher.py every few minutes; also runnable alone:
#   python3 resources.py            check and act
#   python3 resources.py --dry-run  report only
# "In use" means an active (non-archived) Paseo agent mentioned the stack recently, or its worktree changed recently.
# Stopping keeps a stack's volumes, so `supabase start` (or `docker start`) brings it back with its data.
import calendar, glob, json, os, re, subprocess, sys, time
import config

HERE = config.ROOT
LOG = os.path.join(HERE, "resources.log")
STATE = os.path.join(HERE, "resources.json")
DISK_MIN_GB = int(config.ENV.get("DISK_MIN_GB", 150))          # run the cleanup below this much free space
IDLE_H = float(config.ENV.get("STACK_IDLE_HOURS", 6))           # stacks untouched this long, and unmentioned, get stopped
CLEANUP = config.ENV.get("CLEANUP_SCRIPT", os.path.join(HERE, "scripts", "disk_cleanup.sh"))
LOAD_X = float(config.ENV.get("LOAD_OVERLOAD_X", 4))         # load above this many times the core count counts as overloaded
IGNORE_AGENTS = {a for a in [config.COORDINATOR_AGENT] if a}    # the coordinator mentions every stack; don't count it


def sh(*cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def log(*a):
    with open(LOG, "a") as f:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=f)


def machine():
    load = [float(x) for x in sh("sysctl", "-n", "vm.loadavg").strip("{} \n").split()[:3] or [0, 0, 0]]
    m = re.search(r"free percentage: (\d+)%", sh("memory_pressure"))
    df = sh("df", "-g", "/System/Volumes/Data").splitlines()
    free_gb = int(df[1].split()[3]) if len(df) > 1 else -1
    cpus = int(sh("sysctl", "-n", "hw.ncpu").strip() or 1)
    return {"load": load, "cpus": cpus, "mem_free_pct": int(m.group(1)) if m else -1, "disk_free_gb": free_gb}


def recent_agent_text():
    """Last ~3 MB of each active, non-archived Paseo agent's transcript (where agents name the stacks they use)."""
    out = {}
    for f in glob.glob(os.path.expanduser("~/.paseo/agents/*/*.json")):
        try:
            a = json.load(open(f))
        except Exception:
            continue
        if a.get("archivedAt") or any(a["id"].startswith(i) for i in IGNORE_AGENTS) or "parent-agent-id" in str(a.get("labels")):
            continue
        sid = (a.get("runtimeInfo") or {}).get("sessionId") or (a.get("persistence") or {}).get("sessionId")
        # the agent's own transcript plus its subagents' and workflows' (they're often the ones running the stack)
        files = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")) + \
                glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}/subagents/**/*.jsonl"), recursive=True)
        text = []
        for t in files:
            if time.time() - os.path.getmtime(t) > IDLE_H * 3600:
                continue                               # quiet for longer than the idle window: not using anything
            with open(t, "rb") as fh:
                fh.seek(max(0, os.path.getsize(t) - 2_000_000))
                text.append(fh.read().decode("utf8", "ignore"))
        if text:
            out[a["id"][:7]] = "\n".join(text)
    return out


def touched_within(path, hours):
    if not path or not os.path.isdir(path):
        return False
    r = sh("find", path, "(", "-name", "node_modules", "-o", "-name", ".git", ")", "-prune", "-o",
           "-type", "f", "-mmin", f"-{int(hours * 60)}", "-print", "-quit", timeout=30)
    return bool(r.strip())


def stacks():
    """Running dev stacks: Supabase projects (grouped by compose project) and standalone containers."""
    rows = sh("docker", "ps", "--format", "{{.ID}}|{{.Names}}|{{.Label \"com.docker.compose.project\"}}|"
              "{{.Label \"com.supabase.cli.workdir\"}}|{{.RunningFor}}").splitlines()
    groups = {}
    for r in rows:
        cid, name, proj, workdir, age = (r.split("|") + [""] * 5)[:5]
        key = proj or name
        g = groups.setdefault(key, {"name": key, "ids": [], "workdir": workdir, "age": age})
        g["ids"].append(cid)
    for g in groups.values():
        if not g["workdir"]:                           # standalone container: use a bind-mounted folder as its home
            mounts = sh("docker", "inspect", "-f", "{{range .Mounts}}{{.Source}}\n{{end}}", g["ids"][0]).split()
            g["workdir"] = next((m for m in mounts if m.startswith(os.path.expanduser("~"))), "")
    return list(groups.values())


def containers():
    """Running container count, or None when Docker doesn't answer (an overloaded VM looks like zero otherwise)."""
    try:
        r = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=30)
        return len(r.stdout.split()) if r.returncode == 0 else None
    except Exception:
        return None


def started_hours_ago(cid):
    t = sh("docker", "inspect", "-f", "{{.State.StartedAt}}", cid).strip()[:19]
    try:
        return (time.time() - calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%S"))) / 3600   # docker reports UTC
    except Exception:
        return 0


def idle_stacks(agent_text, idle_h=None):
    idle_h = IDLE_H if idle_h is None else idle_h
    idle = []
    for g in stacks():
        name = g["name"]
        short = name[len(config.ENV.get("STACK_PREFIX", "")):] if name.startswith(config.ENV.get("STACK_PREFIX", "") or "\0") else name
        users = [a for a, t in agent_text.items() if name in t or (len(short) > 4 and short in t)]
        if users or touched_within(g["workdir"], idle_h) or started_hours_ago(g["ids"][0]) < idle_h:
            continue
        idle.append(g)
    return idle


def git_storm(dry=False, keep=2):
    """Agents' commits and fetches each start a background `git maintenance --auto`. Worktrees share one object store,
    so dozens can end up repacking the same repo at once (~1 GB of memory and a temp pack each). Keep the oldest
    `keep`, stop the rest. Repacks are atomic, so stopping one leaves the repository intact."""
    rows = sh("ps", "-Ao", "pid=,etimes=,command=").splitlines()
    runs = [r.split(None, 2) for r in rows if "git maintenance run --auto" in r]
    if len(runs) <= keep:
        return None
    extra = sorted(runs, key=lambda r: -int(r[1]))[keep:]          # oldest first, kept
    if not dry:
        for pid, _, _ in extra:
            sh("pkill", "-TERM", "-P", pid)                        # its repack and pack-objects children
            sh("kill", pid)
    return f"stopped {len(extra)} of {len(runs)} concurrent git maintenance runs (kept {keep})"


def run(dry=False):
    m = machine()
    actions = []
    storm = git_storm(dry)
    if storm:
        actions.append(storm)
    # overloaded (run queue several times the core count): stacks idle for even an hour go too, and the
    # coordinator hears about it so it can ask the busiest agents to throttle their local checks
    overloaded = m["load"][0] > LOAD_X * m["cpus"]
    if overloaded:
        actions.append(f"load {m['load'][0]:.0f} on {m['cpus']} cores: overloaded")
    agent_text = recent_agent_text()
    for g in idle_stacks(agent_text, idle_h=1 if overloaded else None):
        if not dry:
            for cid in g["ids"]:
                sh("docker", "stop", cid, timeout=120)  # one at a time: OrbStack rejects batched stops
        actions.append(f"stopped idle dev stack {g['name']} ({len(g['ids'])} containers, volumes kept)")
    if 0 <= m["disk_free_gb"] < DISK_MIN_GB and os.path.exists(CLEANUP):
        out = "" if dry else sh(CLEANUP, timeout=3600)
        freed = re.search(r"freed=(-?\d+)G", out)
        actions.append(f"disk {m['disk_free_gb']}G free: ran safe cleanup" + (f", freed {freed.group(1)}G" if freed else ""))
    m.update(time=time.time(), actions=actions,
             running_containers=containers(),
             top=[l for l in sh("ps", "-Ao", "pcpu,rss,comm", "-r").splitlines()[1:6]])
    json.dump(m, open(STATE, "w"), indent=1)
    for a in actions:
        log(a)
    return m


if __name__ == "__main__":
    r = run(dry="--dry-run" in sys.argv)
    print(json.dumps({k: r[k] for k in ("load", "cpus", "mem_free_pct", "disk_free_gb", "running_containers", "actions")}, indent=1))
