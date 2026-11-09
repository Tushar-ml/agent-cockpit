#!/usr/bin/env python3
"""
Agent Cockpit hook relay.

Installed as a Claude Code hook for every lifecycle event. It forwards the hook
payload (plus host / ssh / token-usage enrichment) to the Agent Cockpit
dashboard server, and for PermissionRequest events it blocks until the
dashboard approves or denies (falling back to the normal terminal prompt when
the dashboard is unreachable, times out, or the user clicks "Ask in terminal").

Stdlib only, Python 3.8+. Never raises: any failure exits 0 with no output so
Claude Code behaves exactly as if this hook did not exist.
"""
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.3.1"
HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".claude", "agent-cockpit")
CONFIG_PATH = os.path.join(BASE, "config.json")
OFFSETS_PATH = os.path.join(BASE, "offsets.json")
SPOOL_PATH = os.path.join(BASE, "spool.jsonl")
SPOOL_MAX_LINES = 2000

USAGE_EVENTS = {
    "SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure",
    "Stop", "StopFailure", "SubagentStop", "PreCompact", "SessionEnd", "Notification",
}


def load_config():
    cfg = {"server": "http://127.0.0.1:7777", "token": "", "label": "", "approval_timeout": 300}
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if os.environ.get("COCKPIT_URL"):
        cfg["server"] = os.environ["COCKPIT_URL"]
    if os.environ.get("COCKPIT_LABEL"):
        cfg["label"] = os.environ["COCKPIT_LABEL"]
    if os.environ.get("COCKPIT_TOKEN"):
        cfg["token"] = os.environ["COCKPIT_TOKEN"]
    if not cfg.get("token"):
        try:
            with open(os.path.join(BASE, "token")) as f:
                cfg["token"] = f.read().strip()
        except Exception:
            pass
    cfg["server"] = cfg["server"].rstrip("/")
    return cfg


def http(cfg, method, path, body=None, timeout=2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(cfg["server"] + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Cockpit-Token", cfg.get("token", ""))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- usage ----

def _lock(f):
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except Exception:
        pass


def read_new_usage(path, max_bytes=8 * 1024 * 1024):
    """Return usage entries appended to a transcript since our last read."""
    if not path or not os.path.isfile(path):
        return []
    os.makedirs(BASE, exist_ok=True)
    entries = []
    with open(OFFSETS_PATH, "a+") as lf:
        _lock(lf)
        lf.seek(0)
        try:
            offsets = json.loads(lf.read() or "{}")
        except Exception:
            offsets = {}
        off = offsets.get(path, 0)
        size = os.path.getsize(path)
        if off > size:
            off = 0
        if size - off > max_bytes:
            # very first read of a huge transcript: only the tail matters for "now",
            # the dashboard's local scanner covers full history on the local host.
            off = size - max_bytes
        with open(path, "rb") as tf:
            tf.seek(off)
            chunk = tf.read(size - off)
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return []
        consumed = chunk[: last_nl + 1]
        seen = {}
        for line in consumed.splitlines():
            if b'"usage"' not in line and b'"type":"user"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            msg = o.get("message") or {}
            if o.get("type") == "assistant" and isinstance(msg.get("usage"), dict):
                u = msg["usage"]
                mid = msg.get("id") or o.get("uuid")
                seen[mid] = {
                    "id": mid,
                    "model": msg.get("model", ""),
                    "in": u.get("input_tokens", 0) or 0,
                    "out": u.get("output_tokens", 0) or 0,
                    "cr": u.get("cache_read_input_tokens", 0) or 0,
                    "cw": u.get("cache_creation_input_tokens", 0) or 0,
                    "cw1": (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0,
                    "ts": o.get("timestamp"),
                    "side": bool(o.get("isSidechain")),
                }
        entries = [e for e in seen.values() if e["model"] != "<synthetic>"]
        offsets[path] = off + last_nl + 1
        lf.seek(0)
        lf.truncate()
        lf.write(json.dumps(offsets))
    return entries


# ---------------------------------------------------------------- spool ----

def spool(event):
    try:
        os.makedirs(BASE, exist_ok=True)
        lines = []
        if os.path.exists(SPOOL_PATH):
            with open(SPOOL_PATH) as f:
                lines = f.readlines()[-SPOOL_MAX_LINES:]
        lines.append(json.dumps(event) + "\n")
        with open(SPOOL_PATH, "w") as f:
            f.writelines(lines)
    except Exception:
        pass


def flush_spool(cfg):
    if not os.path.exists(SPOOL_PATH):
        return
    try:
        tmp = SPOOL_PATH + ".sending"
        os.replace(SPOOL_PATH, tmp)
        with open(tmp) as f:
            batch = [json.loads(l) for l in f if l.strip()]
        try:
            http(cfg, "POST", "/api/events", {"events": batch}, timeout=4)
            os.remove(tmp)
        except Exception:
            with open(tmp) as f:
                old = f.read()
            with open(SPOOL_PATH, "a") as f:
                f.write(old)
            os.remove(tmp)
    except Exception:
        pass


# ---------------------------------------------------------------- main -----

def host_info(cfg):
    ssh = os.environ.get("SSH_CONNECTION", "")
    return {
        "hostname": cfg.get("label") or socket.gethostname().split(".")[0],
        "fqdn": socket.gethostname(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
        "os": f"{platform.system()} {platform.release()}",
        "ssh_from": ssh.split(" ")[0] if ssh else "",
        "ssh": bool(ssh),
        "term": os.environ.get("TERM_PROGRAM", ""),
    }


def handle_permission(cfg, event):
    """Block until the dashboard decides. Return hook stdout dict or None."""
    try:
        res = http(cfg, "POST", "/api/approval", event, timeout=3)
    except Exception:
        return None  # dashboard offline -> normal terminal prompt
    aid = res.get("id")
    if not aid:
        return None
    deadline = time.time() + float(res.get("timeout") or cfg.get("approval_timeout") or 300)
    while time.time() < deadline:
        wait = max(1, min(25, int(deadline - time.time())))
        try:
            d = http(cfg, "GET", f"/api/approval/{aid}/wait?timeout={wait}", timeout=wait + 5)
        except Exception:
            time.sleep(1)
            continue
        b = d.get("behavior")
        if not b or b == "pending":
            continue
        if b == "ask":
            return None
        if b == "allow":
            dec = {"behavior": "allow"}
            if d.get("updatedPermissions"):
                dec["updatedPermissions"] = d["updatedPermissions"]
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": dec}}
        if b == "deny":
            dec = {"behavior": "deny", "message": d.get("message") or "Denied from Agent Cockpit dashboard"}
            if d.get("interrupt"):
                dec["interrupt"] = True
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": dec}}
        return None
    try:
        http(cfg, "POST", f"/api/approval/{aid}/decide", {"behavior": "expired"}, timeout=2)
    except Exception:
        pass
    return None


def ensure_token(cfg):
    if cfg.get("token"):
        return
    try:
        import secrets
        os.makedirs(BASE, exist_ok=True)
        path = os.path.join(BASE, "token")
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(24))
        with open(path) as f:
            cfg["token"] = f.read().strip()
    except Exception:
        pass


def _vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


def maybe_start_server(cfg):
    """Plugin installs ship cli.py next to this file: launch the local dashboard if it is down.
    Never on SSH sessions (those report through the tunnel to your laptop)."""
    if cfg.get("autostart_server") is False or os.environ.get("SSH_CONNECTION") or os.environ.get("COCKPIT_NO_AUTOSTART"):
        return
    server = cfg["server"]
    if not (server.startswith("http://127.0.0.1:") or server.startswith("http://localhost:")):
        return
    cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")
    if not os.path.exists(cli):
        return
    try:
        with urllib.request.urlopen(server + "/healthz", timeout=0.5) as r:
            running = json.loads(r.read() or b"{}").get("version")
        if not running or _vtuple(running) >= _vtuple(VERSION):
            return
        # an older dashboard (e.g. from before a plugin update) is running: replace it
        try:
            http(cfg, "POST", "/api/shutdown", {}, timeout=2)
        except Exception:
            return
        time.sleep(0.6)
    except Exception:
        pass
    try:
        import subprocess
        port = server.rsplit(":", 1)[1].split("/")[0]
        log = open(os.path.join(BASE, "server.log"), "a")
        subprocess.Popen([sys.executable, cli, "serve", "--port", port], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            time.sleep(0.1)
            try:
                urllib.request.urlopen(server + "/healthz", timeout=0.3)
                return
            except Exception:
                pass
    except Exception:
        pass


def statusline():
    """Statusline relay: forward Claude Code's statusline JSON (account rate limits, session cost)
    to the dashboard, then render the user's original statusline (or a compact default)."""
    raw = sys.stdin.read()
    cfg = load_config()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    try:
        http(cfg, "POST", "/api/statusline", {"payload": data, "host": host_info(cfg), "ts": time.time()}, timeout=0.4)
    except Exception:
        pass
    orig = cfg.get("statusline_command")
    if orig:
        try:
            import subprocess
            r = subprocess.run(orig, shell=True, input=raw, capture_output=True, text=True, timeout=10)
            sys.stdout.write(r.stdout)
            return
        except Exception:
            pass
    parts = []
    m = (data.get("model") or {}).get("display_name")
    if m:
        parts.append(m)
    rl = data.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = rl.get(key) or {}
        if w.get("used_percentage") is not None:
            parts.append(f"{label} {w['used_percentage']:.0f}%")
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if cost:
        parts.append(f"${cost:.2f}")
    sys.stdout.write(" · ".join(parts))


def main():
    if "--statusline" in sys.argv:
        return statusline()
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return
    cfg = load_config()
    name = payload.get("hook_event_name", "")
    if name == "SessionStart":
        ensure_token(cfg)
        maybe_start_server(cfg)
    event = {
        "payload": payload,
        "host": host_info(cfg),
        "ts": time.time(),
        "pid": os.getppid(),
    }
    if name in USAGE_EVENTS:
        try:
            event["usage"] = read_new_usage(payload.get("transcript_path"))
            atp = payload.get("agent_transcript_path")
            if atp:
                for e in read_new_usage(atp):
                    e["side"] = True
                    e["agent_id"] = payload.get("agent_id")
                    event["usage"].append(e)
        except Exception:
            pass

    if name == "PermissionRequest":
        out = handle_permission(cfg, event)
        if out:
            sys.stdout.write(json.dumps(out))
        return

    try:
        http(cfg, "POST", "/api/event", event, timeout=1.5)
        flush_spool(cfg)
    except Exception:
        spool(event)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
