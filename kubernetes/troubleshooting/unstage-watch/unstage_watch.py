#!/usr/bin/env python3
"""Watch CSI block volumes being unstaged and report anything that keeps the
filesystem alive after the kubelet's staging mount is gone.

See docs/iscsi-unstage-data-loss.md ("Unstage Watcher") for how to run it and
what each event means. Output is one JSON object per line on stdout.
"""

import errno
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone

PROC = os.environ.get("PROC", "/proc")
SYS = os.environ.get("SYS", "/sys")
PODS_LOG_DIR = os.environ.get("PODS_LOG_DIR", "/host/var/log/pods")
KMSG = os.environ.get("KMSG", "/dev/kmsg")
NODE = os.environ.get("NODE_NAME") or os.uname().nodename
DEVICE_SYSFS_RE = re.compile(os.environ.get("DEVICE_SYSFS_RE", r"/session\d+/"))
FAST_INTERVAL = float(os.environ.get("FAST_INTERVAL", "0.05"))
BURST_INTERVAL = float(os.environ.get("BURST_INTERVAL", "0.1"))
BURST_SECONDS = float(os.environ.get("BURST_SECONDS", "20"))
IDLE_INTERVAL = float(os.environ.get("IDLE_INTERVAL", "2"))
HIDDEN_GRACE = float(os.environ.get("HIDDEN_GRACE", "0.3"))
HEARTBEAT_SECONDS = float(os.environ.get("HEARTBEAT_SECONDS", "600"))
MAX_PIDS_PER_NS = int(os.environ.get("MAX_PIDS_PER_NS", "8"))
RECENT_SECONDS = 120

STAGING_RE = re.compile(r"/plugins/kubernetes\.io/csi/(?P<driver>[^/]+)/(?P<hash>[0-9a-f]+)/globalmount$")
PUBLISH_RE = re.compile(r"/pods/(?P<uid>[0-9a-f-]{36})/volumes/kubernetes\.io~csi/(?P<pv>[^/]+)/mount$")
CGROUP_POD_RE = re.compile(
    r"pod(?P<uid>[0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12})"
)
CGROUP_CID_RE = re.compile(r"(?:cri-containerd-)?(?P<cid>[0-9a-f]{64})(?:\.scope)?$")
KMSG_DEV_RE = re.compile(
    r"XFS \((?P<a>sd[a-z]+)\)|\b(?P<b>sd[a-z]+): writeback error|\bdev (?P<c>sd[a-z]+)\b|\[(?P<d>sd[a-z]+)\]"
)
KMSG_ISCSI_RE = re.compile(r"\bconnection\d+:\d+|\bsession\d+|iscsi", re.I)
KMSG_BAD_RE = re.compile(r"removal|error|offline|shut ?down|failed|recovery", re.I)

_out = threading.Lock()


def ts():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(level, event, msg, **fields):
    rec = {"ts": ts(), "level": level, "event": event, "node": NODE, "msg": msg}
    rec.update(fields)
    line = json.dumps(rec, default=str, separators=(",", ":"))
    with _out:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def mount_kind(mountpoint):
    if STAGING_RE.search(mountpoint):
        return "staging"
    if PUBLISH_RE.search(mountpoint):
        return "publish"
    return "other"


def scan_devices():
    """Block devices whose sysfs path matches DEVICE_SYSFS_RE (iSCSI by default)."""
    devs = {}
    try:
        names = os.listdir(f"{SYS}/block")
    except OSError:
        return devs
    for name in names:
        real = os.path.realpath(f"{SYS}/block/{name}")
        if not DEVICE_SYSFS_RE.search(real):
            continue
        majmin = (read(f"{SYS}/block/{name}/dev") or "").strip()
        if not majmin:
            continue
        m = re.search(r"/(session\d+)/", real)
        session = m.group(1) if m else None
        target = None
        if session:
            target = (read(f"{SYS}/class/iscsi_session/{session}/targetname") or "").strip() or None
        devs[majmin] = {"dev": name, "majmin": majmin, "session": session, "target": target}
    return devs


def fs_alive(dev):
    """True while the kernel still has a superblock for this device."""
    return any(os.path.isdir(f"{SYS}/fs/{fs}/{dev}") for fs in ("xfs", "ext4"))


def scan_namespaces():
    nsmap = {}
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        try:
            ns = os.readlink(f"{PROC}/{name}/ns/mnt")
        except OSError:
            continue
        nsmap.setdefault(ns, []).append(int(name))
    return nsmap


def ns_mountinfo(pids):
    for pid in pids[:3]:
        text = read(f"{PROC}/{pid}/mountinfo")
        if text is not None:
            return text
    return None


def parse_mountinfo(text, wanted):
    out = []
    for line in text.splitlines():
        parts = line.split(" ")
        if len(parts) < 10 or parts[2] not in wanted:
            continue
        try:
            sep = parts.index("-", 6)
        except ValueError:
            continue
        out.append(
            {
                "majmin": parts[2],
                "root": unescape(parts[3]),
                "mountpoint": unescape(parts[4]),
                "propagation": " ".join(parts[6:sep]) or "private",
                "fstype": parts[sep + 1],
            }
        )
    return out


def find_kubelet():
    for name in os.listdir(PROC):
        if name.isdigit() and (read(f"{PROC}/{name}/comm") or "").strip() == "kubelet":
            return int(name)
    return None


class PodNames:
    """Pod UID -> namespace/name, from the /var/log/pods directory names."""

    def __init__(self):
        self.cache = {}

    def refresh(self):
        try:
            entries = os.listdir(PODS_LOG_DIR)
        except OSError:
            return
        for entry in entries:
            parts = entry.split("_")
            if len(parts) == 3:
                self.cache[parts[2]] = f"{parts[0]}/{parts[1]}"

    def get(self, uid):
        if uid not in self.cache:
            self.refresh()
        return self.cache.get(uid)


def proc_info(pid, pods):
    stat = read(f"{PROC}/{pid}/stat")
    state = stat.rsplit(")", 1)[1].split()[0] if stat and ")" in stat else None
    cgroup = (read(f"{PROC}/{pid}/cgroup") or "").strip().splitlines()
    cgpath = cgroup[-1].split(":", 2)[-1] if cgroup else None
    info = {
        "pid": pid,
        "comm": (read(f"{PROC}/{pid}/comm") or "").strip(),
        "state": state,
        "cmdline": (read(f"{PROC}/{pid}/cmdline") or "").replace("\0", " ").strip()[:200],
        "cgroup": cgpath,
    }
    if cgpath:
        m = CGROUP_POD_RE.search(cgpath)
        if m:
            uid = m.group("uid").replace("_", "-")
            info["pod_uid"] = uid
            info["pod"] = pods.get(uid)
        c = CGROUP_CID_RE.search(cgpath)
        if c:
            info["container_id"] = c.group("cid")[:13]
    return info


def deep_scan(majmin, nsmap):
    """Look for references that do not show up as mounts: open files, cwd/root,
    memory maps, and mount namespaces pinned by an fd or nsfs bind mount with
    no process left in them."""
    major, minor = (int(x) for x in majmin.split(":"))
    devnum = os.makedev(major, minor)
    hexdev = f"{major:02x}:{minor:02x}"
    live_ns = set(nsmap)
    users, fd_pins, nsfs_pins = [], [], []
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        base = f"{PROC}/{name}"
        hits = []
        for what in ("cwd", "root"):
            try:
                if os.stat(f"{base}/{what}").st_dev == devnum:
                    hits.append(what)
            except OSError:
                pass
        try:
            fds = os.listdir(f"{base}/fd")
        except OSError:
            fds = []
        for fd in fds:
            path = f"{base}/fd/{fd}"
            try:
                target = os.readlink(path)
            except OSError:
                continue
            if target.startswith("mnt:["):
                if target not in live_ns:
                    fd_pins.append({"pid": int(name), "fd": int(fd), "ns": target})
                continue
            if not target.startswith("/"):
                continue
            try:
                if os.stat(path).st_dev == devnum:
                    hits.append(f"fd {fd} {target}")
            except OSError:
                pass
        for line in (read(f"{base}/maps") or "").splitlines():
            f = line.split(None, 5)
            if len(f) >= 4 and f[3] == hexdev:
                hits.append("mmap " + (f[5] if len(f) > 5 else ""))
        if hits:
            users.append({"pid": int(name), "uses": sorted(set(hits))[:10]})
    for ns, pids in nsmap.items():
        for line in (ns_mountinfo(pids) or "").splitlines():
            parts = line.split(" ")
            try:
                sep = parts.index("-", 6)
            except ValueError:
                continue
            root = unescape(parts[3])
            if parts[sep + 1] == "nsfs" and root.startswith("mnt:[") and root not in live_ns:
                nsfs_pins.append({"in_ns": ns, "mountpoint": unescape(parts[4]), "pins": root})
    return users, fd_pins, nsfs_pins


class Watcher:
    def __init__(self):
        self.pods = PodNames()
        self.tracks = {}
        self.recent = {}
        self.lock = threading.Lock()
        self.kubelet_pid = None
        self.kubelet_retry_at = 0.0
        self.started = False
        self.last_scan_ms = 0.0
        self.last_ns_count = 0

    def full_scan(self):
        t0 = time.monotonic()
        devs = scan_devices()
        nsmap = scan_namespaces()
        self.pods.refresh()
        mounts = {mm: [] for mm in devs}
        wanted = set(devs)
        for ns, pids in nsmap.items():
            text = ns_mountinfo(pids)
            if not text:
                continue
            for m in parse_mountinfo(text, wanted):
                m["ns"] = ns
                mounts[m["majmin"]].append(m)
        alive = {mm: fs_alive(d["dev"]) for mm, d in devs.items()}
        self.last_scan_ms = round((time.monotonic() - t0) * 1000, 1)
        self.last_ns_count = len(nsmap)
        return devs, nsmap, mounts, alive

    def kubelet_mounts(self):
        """Cheap per-tick view of staging and publish mounts in the kubelet's namespace."""
        pid = self.kubelet_pid
        if pid is None or (read(f"{PROC}/{pid}/comm") or "").strip() != "kubelet":
            if time.monotonic() < self.kubelet_retry_at:
                return None
            pid = self.kubelet_pid = find_kubelet()
            if pid is None:
                self.kubelet_retry_at = time.monotonic() + 5
                return None
        text = read(f"{PROC}/{pid}/mountinfo")
        if text is None:
            self.kubelet_pid = None
            return None
        return frozenset(
            line.split(" ", 5)[4]
            for line in text.splitlines()
            if "/globalmount " in line or "/volumes/kubernetes.io~csi/" in line
        )

    def ident(self, tr):
        info = tr["info"]
        return {
            "dev": info["dev"],
            "majmin": info["majmin"],
            "pv": tr["pv"],
            "driver": tr["driver"],
            "target": info["target"],
            "consumer_pods": sorted(self.pods.get(u) or u for u in tr["consumers"]),
        }

    def short_owner(self, pids):
        if not pids:
            return None
        info = proc_info(min(pids), self.pods)
        return {
            "pid": info["pid"],
            "comm": info["comm"],
            "pod": info.get("pod"),
            "container_id": info.get("container_id"),
            "pid_count": len(pids),
        }

    def mount_view(self, m, owner):
        return {
            "ns": m["ns"],
            "mountpoint": m["mountpoint"],
            "kind": mount_kind(m["mountpoint"]),
            "propagation": m["propagation"],
            "owner": owner(m["ns"]),
        }

    def holders(self, mounts, nsmap, tr):
        by_ns = {}
        for m in mounts:
            by_ns.setdefault(m["ns"], []).append(
                {"mountpoint": m["mountpoint"], "kind": mount_kind(m["mountpoint"]), "propagation": m["propagation"]}
            )
        out = []
        for ns, ms in by_ns.items():
            pids = sorted(nsmap.get(ns, []))
            procs = [proc_info(p, self.pods) for p in pids[:MAX_PIDS_PER_NS]]
            for p in procs:
                if p.get("pod_uid") in tr["consumers"]:
                    p["consumer_pod"] = True
            out.append({"ns": ns, "ns_alive": ns in nsmap, "pid_count": len(pids), "mounts": ms, "processes": procs})
        return out

    def summary(self, tr, now):
        since = None if tr["unstaged_at"] is None else round((now - tr["unstaged_at"]) * 1000)
        return {
            **self.ident(tr),
            "staged": tr["staged"],
            "ms_since_unstage": since,
            "mounts": len(tr["mounts"]),
            "fs_alive": tr["alive"],
            "holder_ns": sorted({m["ns"] for m in tr["mounts"]}),
        }

    def recent_summary(self, dev):
        now = time.monotonic()
        for tr in self.tracks.values():
            if tr["info"]["dev"] == dev:
                return self.summary(tr, now)
        hit = self.recent.get(dev)
        if hit and hit[0] > now:
            return hit[1]
        return None

    def unsettled(self, now):
        return any(
            tr["unstaged_at"] is not None and now - tr["unstaged_at"] < RECENT_SECONDS and (tr["alive"] or tr["mounts"])
            for tr in self.tracks.values()
        )

    def new_track(self, dev):
        return {
            "info": dev,
            "keys": set(),
            "mounts": [],
            "staged": False,
            "unstaged_at": None,
            "alive": False,
            "pv": None,
            "driver": None,
            "consumers": set(),
            "hidden_checked": False,
            "released_reported": False,
        }

    def process(self, devs, nsmap, mounts, alive, now=None):
        now = time.monotonic() if now is None else now
        owners = {}

        def owner(ns):
            if ns not in owners:
                owners[ns] = self.short_owner(nsmap.get(ns, []))
            return owners[ns]

        for mm in [mm for mm in self.tracks if mm not in devs]:
            self.device_gone(mm, now)

        for mm, dev in devs.items():
            tr = self.tracks.get(mm)
            if tr is None:
                tr = self.tracks[mm] = self.new_track(dev)
                if self.started:
                    emit("info", "device_added", f"{dev['dev']} attached", **self.ident(tr))
            cur = mounts.get(mm, [])
            is_alive = alive.get(mm, False)

            for m in cur:
                p = PUBLISH_RE.search(m["mountpoint"])
                if p:
                    tr["pv"] = p["pv"]
                    tr["consumers"].add(p["uid"])
                s = STAGING_RE.search(m["mountpoint"])
                if s:
                    tr["driver"] = s["driver"]

            keys = {(m["ns"], m["mountpoint"]) for m in cur}
            if self.started and keys != tr["keys"]:
                added = [self.mount_view(m, owner) for m in cur if (m["ns"], m["mountpoint"]) not in tr["keys"]]
                removed = [self.mount_view(m, owner) for m in tr["mounts"] if (m["ns"], m["mountpoint"]) not in keys]
                emit(
                    "debug",
                    "mounts_changed",
                    f"{dev['dev']}: +{len(added)} -{len(removed)} mounts, {len(cur)} now",
                    **self.ident(tr),
                    added=added,
                    removed=removed,
                    fs_alive=is_alive,
                )

            staged = any(mount_kind(m["mountpoint"]) == "staging" for m in cur)
            if staged:
                tr["unstaged_at"] = None
                tr["hidden_checked"] = False
                tr["released_reported"] = False
            elif tr["staged"] and self.started:
                tr["unstaged_at"] = now
                if cur:
                    emit(
                        "warn",
                        "still_mounted_after_unstage",
                        f"{dev['dev']}: staging mount is gone but {len(cur)} mount(s) remain in other mount namespaces",
                        **self.ident(tr),
                        fs_alive=is_alive,
                        holders=self.holders(cur, nsmap, tr),
                    )
                else:
                    emit(
                        "info",
                        "staging_unmounted",
                        f"{dev['dev']}: staging mount is gone and no other mounts remain",
                        **self.ident(tr),
                        fs_alive=is_alive,
                    )
            elif cur and tr["unstaged_at"] is not None and keys != tr["keys"]:
                emit(
                    "warn",
                    "still_mounted_after_unstage",
                    f"{dev['dev']}: {len(cur)} mount(s) still remain",
                    **self.ident(tr),
                    fs_alive=is_alive,
                    ms_since_unstage=round((now - tr["unstaged_at"]) * 1000),
                    holders=self.holders(cur, nsmap, tr),
                )

            if tr["unstaged_at"] is not None:
                age = now - tr["unstaged_at"]
                if not is_alive and not tr["released_reported"]:
                    tr["released_reported"] = True
                    emit(
                        "info",
                        "filesystem_released",
                        f"{dev['dev']}: filesystem released {age * 1000:.0f}ms after the staging unmount",
                        **self.ident(tr),
                        ms_since_unstage=round(age * 1000),
                        mounts_left=len(cur),
                    )
                elif is_alive and not cur and not tr["hidden_checked"] and age >= HIDDEN_GRACE:
                    tr["hidden_checked"] = True
                    emit(
                        "warn",
                        "alive_without_mounts",
                        f"{dev['dev']}: filesystem still alive {age * 1000:.0f}ms after unstage with no mount "
                        "in any process's namespace; running a deep scan",
                        **self.ident(tr),
                        ms_since_unstage=round(age * 1000),
                    )
                    threading.Thread(
                        target=self.run_deep_scan, args=(dict(tr["info"]), self.ident(tr), dict(nsmap)), daemon=True
                    ).start()

            tr["staged"] = staged
            tr["keys"] = keys
            tr["mounts"] = cur
            tr["alive"] = is_alive
            self.recent[dev["dev"]] = (now + RECENT_SECONDS, self.summary(tr, now))

        for name in [n for n, (exp, _) in self.recent.items() if exp < now]:
            del self.recent[name]
        self.started = True

    def device_gone(self, mm, now):
        tr = self.tracks.pop(mm)
        dev = tr["info"]["dev"]
        since = None if tr["unstaged_at"] is None else round((now - tr["unstaged_at"]) * 1000)
        if tr["alive"] or tr["mounts"]:
            emit(
                "error",
                "device_removed_while_mounted",
                f"{dev}: device removed while its filesystem was still alive",
                **self.ident(tr),
                ms_since_unstage=since,
                still_staged=tr["staged"],
                fs_alive_last_seen=tr["alive"],
                holders=self.holders(tr["mounts"], scan_namespaces(), tr),
            )
        else:
            emit("info", "device_removed", f"{dev}: detached cleanly", **self.ident(tr), ms_since_unstage=since)
        self.recent[dev] = (now + RECENT_SECONDS, self.summary(tr, now))

    def run_deep_scan(self, info, ident, nsmap):
        try:
            users, fd_pins, nsfs_pins = deep_scan(info["majmin"], nsmap)
        except Exception as e:
            emit("warn", "deep_scan_failed", f"{info['dev']}: {e!r}", **ident)
            return
        emit(
            "warn",
            "deep_scan",
            f"{info['dev']}: {len(users)} process(es) using the device, "
            f"{len(fd_pins)} fd pin(s) and {len(nsfs_pins)} nsfs pin(s) on process-less mount namespaces",
            **ident,
            fs_alive_now=fs_alive(info["dev"]),
            users=[{**u, **proc_info(u["pid"], self.pods)} for u in users],
            fd_pins=[{**p, "holder": proc_info(p["pid"], self.pods)} for p in fd_pins],
            nsfs_pins=nsfs_pins,
        )

    def snapshot(self):
        devs, nsmap, mounts, alive = self.full_scan()
        self.process(devs, nsmap, mounts, alive)
        for mm, tr in sorted(self.tracks.items(), key=lambda kv: kv[1]["info"]["dev"]):
            emit(
                "info",
                "snapshot",
                f"{tr['info']['dev']}: {len(tr['mounts'])} mounts",
                **self.ident(tr),
                fs_alive=tr["alive"],
                holders=self.holders(tr["mounts"], nsmap, tr),
            )
        emit("info", "snapshot_done", f"{len(devs)} devices, {len(nsmap)} mount namespaces", scan_ms=self.last_scan_ms)


def kmsg_loop(w):
    try:
        fd = os.open(KMSG, os.O_RDONLY)
        os.lseek(fd, 0, os.SEEK_END)
    except OSError as e:
        emit("warn", "kmsg_unavailable", f"cannot read {KMSG}: {e}")
        return
    while True:
        try:
            record = os.read(fd, 8192).decode(errors="replace")
        except OSError as e:
            if e.errno == errno.EPIPE:
                continue
            emit("warn", "kmsg_error", f"reading {KMSG}: {e}")
            time.sleep(5)
            continue
        msg = record.partition(";")[2].split("\n", 1)[0]
        if "audit" in msg or "avc:" in msg:
            continue
        m = KMSG_DEV_RE.search(msg)
        dev = next((g for g in m.groups() if g), None) if m else None
        if dev is None and not KMSG_ISCSI_RE.search(msg):
            continue
        with w.lock:
            tracked = w.recent_summary(dev) if dev else None
        emit("error" if KMSG_BAD_RE.search(msg) else "info", "kernel", msg, dev=dev, tracked=tracked)


def main():
    w = Watcher()
    if "--once" in sys.argv:
        w.snapshot()
        return

    with w.lock:
        w.process(*w.full_scan())
    prev = w.kubelet_mounts()
    emit(
        "info",
        "started",
        f"watching {len(w.tracks)} devices across {w.last_ns_count} mount namespaces",
        kubelet_pid=w.kubelet_pid,
        scan_ms=w.last_scan_ms,
        devices=[{**w.ident(t), "mounts": len(t["mounts"]), "fs_alive": t["alive"]} for t in w.tracks.values()],
        config={
            "device_sysfs_re": DEVICE_SYSFS_RE.pattern,
            "fast_interval": FAST_INTERVAL,
            "burst_interval": BURST_INTERVAL,
            "burst_seconds": BURST_SECONDS,
            "idle_interval": IDLE_INTERVAL,
            "hidden_grace": HIDDEN_GRACE,
        },
    )
    threading.Thread(target=kmsg_loop, args=(w,), daemon=True).start()

    now = time.monotonic()
    next_full = now + IDLE_INTERVAL
    next_heartbeat = now + HEARTBEAT_SECONDS
    burst_until = 0.0
    while True:
        time.sleep(FAST_INTERVAL)
        now = time.monotonic()
        cur = w.kubelet_mounts()
        changed = cur is not None and cur != prev
        if changed:
            burst_until = now + BURST_SECONDS
        if changed or now >= next_full:
            with w.lock:
                w.process(*w.full_scan())
                fast = cur is None or now < burst_until or w.unsettled(now)
            next_full = now + (BURST_INTERVAL if fast else IDLE_INTERVAL)
        if cur is not None:
            prev = cur
        if now >= next_heartbeat:
            next_heartbeat = now + HEARTBEAT_SECONDS
            emit(
                "info",
                "heartbeat",
                f"{len(w.tracks)} devices, {w.last_ns_count} mount namespaces, last scan {w.last_scan_ms}ms",
                kubelet_pid=w.kubelet_pid,
                scan_ms=w.last_scan_ms,
            )


if __name__ == "__main__":
    main()
