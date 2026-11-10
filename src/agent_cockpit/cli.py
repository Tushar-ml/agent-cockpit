#!/usr/bin/env python3
"""
Agent Cockpit — a live dashboard for every Claude Code session you run,
on this machine and on any machine you SSH into.

  agent-cockpit setup                              hooks + start at login + open the dashboard
  agent-cockpit serve [--port 7777] [--open]       run the dashboard in the foreground
  agent-cockpit start | stop                       run / stop the dashboard in the background
  agent-cockpit install [--server URL --token T]   add hooks to ~/.claude/settings.json
  agent-cockpit uninstall                          remove our hooks (and login item)
  agent-cockpit connect user@host [ssh args...]    set up a remote box + ssh with a tunnel
  agent-cockpit autostart on|off                   start the dashboard at login (macOS / systemd)
  agent-cockpit status                             show config / server health

Stdlib only, Python 3.8+.
"""
import argparse
import glob
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
BASE = os.path.join(CLAUDE_DIR, "agent-cockpit")
TOKEN_PATH = os.path.join(BASE, "token")
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
SETTINGS_PATH = os.path.join(CLAUDE_DIR, "settings.json")
HOOK_MARK = "agent-cockpit/hook.py"
DEFAULT_PORT = 7777
__version__ = "1.4.0"

MAX_EVENTS = 600
SCAN_DAYS = 3


def ensure_token():
    os.makedirs(BASE, exist_ok=True)
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            t = f.read().strip()
            if t:
                return t
    t = secrets.token_urlsafe(24)
    with open(TOKEN_PATH, "w") as f:
        f.write(t)
    os.chmod(TOKEN_PATH, 0o600)
    return t


def local_hostname():
    return socket.gethostname().split(".")[0]


# =========================================================== summaries ====

def short(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_summary(name, inp):
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return inp.get("description") and f"{inp.get('description')} — $ {short(inp.get('command',''), 120)}" or "$ " + short(inp.get("command", ""))
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        return p
    if name in ("Grep", "Glob"):
        return f"{inp.get('pattern','')}" + (f"  in {inp.get('path')}" if inp.get("path") else "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name in ("Task", "Agent"):
        return f"[{inp.get('subagent_type','general-purpose')}] {inp.get('description','')}"
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        done = sum(1 for t in todos if t.get("status") == "completed")
        return f"{done}/{len(todos)} todos done"
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        return qs[0].get("question", "") if qs else ""
    if name == "Skill":
        return inp.get("skill", "")
    for v in inp.values():
        if isinstance(v, str) and v:
            return short(v)
    return ""


def response_brief(resp):
    if isinstance(resp, dict):
        for k in ("stderr", "error"):
            if resp.get(k):
                return short(resp[k], 200)
        for k in ("stdout", "content", "result", "output"):
            if isinstance(resp.get(k), str) and resp[k]:
                return short(resp[k], 200)
        if isinstance(resp.get("file"), dict):
            f = resp["file"]
            return f"{f.get('numLines', '')} lines" if f.get("numLines") else ""
    if isinstance(resp, str):
        return short(resp, 200)
    if isinstance(resp, list) and resp and isinstance(resp[0], dict):
        return short(resp[0].get("text", ""), 200)
    return ""


# ============================================================ pricing ======
# USD per million tokens at Anthropic API list prices: (input, output, cache read).
# Cache writes: 1.25x input for 5-minute, 2x input for 1-hour. Subscription plans are not billed
# per token, so for Pro/Max users these are "API-equivalent" estimates.
PRICES = [
    ("fable-5-1", (10.0, 50.0, 0.25)), ("mythos-5-1", (10.0, 50.0, 0.25)),
    ("fable-5", (10.0, 50.0, 1.0)), ("mythos-5", (10.0, 50.0, 1.0)), ("fable", (10.0, 50.0, 0.25)),
    ("opus-5-5", (4.0, 20.0, 0.20)), ("opus-5", (5.0, 25.0, 0.50)),
    ("opus-4-8", (5.0, 25.0, 0.50)), ("opus-4-7", (5.0, 25.0, 0.50)), ("opus-4-6", (5.0, 25.0, 0.50)),
    ("opus-4-5", (5.0, 25.0, 0.50)), ("opus-4-1", (15.0, 75.0, 1.50)), ("opus-4", (15.0, 75.0, 1.50)),
    ("opus", (4.0, 20.0, 0.20)),
    ("sonnet-5-5", (2.0, 10.0, 0.20)), ("sonnet-5", (2.0, 10.0, 0.20)),
    ("sonnet-4-6", (3.0, 15.0, 0.30)), ("sonnet-4-5", (3.0, 15.0, 0.30)), ("sonnet-4", (3.0, 15.0, 0.30)),
    ("sonnet", (2.0, 10.0, 0.20)),
    ("haiku-5-5", (0.10, 0.50, 0.01)), ("haiku-4-5", (1.0, 5.0, 0.10)), ("haiku-3-5", (0.80, 4.0, 0.08)),
    ("haiku", (0.10, 0.50, 0.01)),
]


def price_of(model):
    m = (model or "").lower()
    if m and "claude" not in m and not any(k in m for k in ("opus", "sonnet", "haiku", "fable", "mythos")):
        return (0.0, 0.0, 0.0)  # non-Anthropic model routed through Claude Code: no Anthropic cost
    for key, p in PRICES:
        if key in m:
            return p
    return PRICES[5][1]  # unknown -> current Opus


def cost_of(e):
    pin, pout, pcr = price_of(e.get("model"))
    cw = int(e.get("cw") or 0)
    cw1 = min(cw, int(e.get("cw1") or 0))
    return (int(e.get("in") or 0) * pin + int(e.get("out") or 0) * pout + int(e.get("cr") or 0) * pcr
            + (cw - cw1) * pin * 1.25 + cw1 * pin * 2.0) / 1e6


def _local_day(ts):
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")


ACCOUNT_VERSION = 2


def _empty_account():
    return {"v": ACCOUNT_VERSION, "daily": {}, "models": {}, "projects": {}, "hosts": {}, "first": None, "last": None}


FILE_TOOLS = {"Read": "r", "Edit": "e", "MultiEdit": "e", "Write": "w", "NotebookEdit": "e"}
MAX_FILES = 600


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
_SEGMENT_BREAK = {"&&", "||", ";", "|", "&", "(", ")"}
_READERS = {"cat", "head", "tail", "less", "more", "wc", "bat", "nl"}
_PREFIXES = {"sudo", "env", "time", "command", "nohup", "exec"}
_PY_ASSIGN = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*(['\"])([^'\"\n]+)\2")
_PY_OPEN_W = re.compile(r"open\(\s*(?:(['\"])([^'\"\n]+)\1|([A-Za-z_]\w*))\s*,\s*['\"]([wax])")
_PY_WRITE_TEXT = re.compile(r"Path\(\s*(['\"])([^'\"\n]+)\1\s*\)\.write_(?:text|bytes)")


_HOSTPORT = re.compile(r"^[\w.-]+:\d+(/|$)")


def _looks_like_path(t):
    return (bool(t) and not t.startswith("-") and len(t) < 400 and not any(c in t for c in "*?$`{}\n")
            and not t.startswith("/dev/") and not t.isdigit() and ("." in t or "/" in t)
            and "://" not in t and not _HOSTPORT.match(t) and not t.endswith(","))


def _is_break(t):
    # shlex glues adjacent punctuation (e.g. ");" or "|&"), so treat any all-punctuation token with a separator as a break
    return t in _SEGMENT_BREAK or (bool(t) and set(t) <= set(";&|()<>") and any(c in t for c in ";|()") ) or t == "&&"


def _resolve(t, base):
    t = os.path.expanduser(t)
    return os.path.normpath(t if os.path.isabs(t) else os.path.join(base or "/", t))


def _split_heredocs(cmd):
    """Split shell text into logical lines [(line, [heredoc bodies])].
    Newlines inside quotes stay part of the line (multi-line strings such as commit messages),
    and heredoc bodies are cut out so their contents are never parsed as commands."""
    out, buf, quote, i, n = [], [], None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 1
            elif c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
        elif c == "\\" and i + 1 < n:
            buf.append(c + cmd[i + 1])
            i += 1
        elif c == "\n":
            line = "".join(buf)
            buf = []
            i += 1
            bodies = []
            for m in _HEREDOC.finditer(line):
                body = []
                while i < n:
                    j = cmd.find("\n", i)
                    j = n if j < 0 else j
                    row = cmd[i:j]
                    i = j + 1
                    if row.strip() == m.group(2):
                        break
                    body.append(row)
                bodies.append("\n".join(body))
            out.append((line, bodies))
            continue
        else:
            buf.append(c)
        i += 1
    if buf:
        out.append(("".join(buf), []))
    return out


def _python_writes(code, base):
    names = {m.group(1): m.group(3) for m in _PY_ASSIGN.finditer(code)}
    found = []
    for m in _PY_OPEN_W.finditer(code):
        target = m.group(2) or names.get(m.group(3) or "")
        if target and _looks_like_path(target):
            found.append((_resolve(target, base), "w" if m.group(4) in "wx" else "e"))
    for m in _PY_WRITE_TEXT.finditer(code):
        if _looks_like_path(m.group(2)):
            found.append((_resolve(m.group(2), base), "w"))
    # a file that was also read in the same script (read-modify-write) is an edit, not a create
    return [(pth, "e" if k == "w" and re.search(r"open\(\s*(?:['\"]" + re.escape(os.path.basename(pth)) + r"|\w+\s*\))\.read", code) else k)
            for pth, k in found]


def shell_file_ops(cmd, cwd, verify_python=False):
    """Best-effort: which files does a shell command write ('w' create/overwrite, 'e' modify) or read ('r')?
    Covers redirects, tee, sed -i, perl -i, cp/mv/touch, cat/head/tail, and python -c / heredoc open(..., 'w')."""
    import shlex
    ops, base = [], cwd
    for line, bodies in _split_heredocs(cmd or ""):
        try:
            lx = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
            lx.whitespace_split = True
            lx.commenters = ""
            toks = list(lx)
        except ValueError:
            continue
        segs, cur = [], []
        for t in toks:
            if _is_break(t):
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(t)
        if cur:
            segs.append(cur)
        for seg in segs:
            words, j = [], 0
            while j < len(seg):  # pull out redirections
                t = seg[j]
                if t in (">", ">>", "&>", ">|") or (t.endswith(">") and t[:-1].isdigit()) or t.endswith(">>") and t[:-2].isdigit():
                    tgt = seg[j + 1] if j + 1 < len(seg) else ""
                    if tgt in (">", "&") and j + 2 < len(seg):  # 2>&1 style
                        j += 3
                        continue
                    if _looks_like_path(tgt) and not t[:1].isdigit():
                        ops.append((_resolve(tgt, base), "e" if t.endswith(">>") else "w"))
                    j += 2
                    continue
                if t in ("<", "<<", "<<<", "<<-"):
                    j += 2
                    continue
                words.append(t)
                j += 1
            while words and ("=" in words[0] and not words[0].startswith("-") or words[0] in _PREFIXES):
                words = words[1:]
            if not words:
                continue
            prog, args = os.path.basename(words[0]), words[1:]
            if prog == "cd" and args:
                base = _resolve(args[0], base)
            elif prog == "tee":
                kind = "e" if "-a" in args or "--append" in args else "w"
                ops += [(_resolve(a, base), kind) for a in args if _looks_like_path(a)]
            elif prog in ("sed", "gsed") and any(a.startswith("-i") or a == "--in-place" for a in args):
                rest, script_seen, k = [], any(a in ("-e", "--expression") for a in args), 0
                while k < len(args):
                    a = args[k]
                    if a in ("-e", "--expression", "-f"):
                        k += 2
                        continue
                    if a == "-i" and k + 1 < len(args) and args[k + 1] == "":
                        k += 2
                        continue
                    if a.startswith("-"):
                        k += 1
                        continue
                    if not script_seen:
                        script_seen = True
                    else:
                        rest.append(a)
                    k += 1
                ops += [(_resolve(a, base), "e") for a in rest if _looks_like_path(a)]
            elif prog == "perl" and any(a.startswith("-") and "i" in a and not a.startswith("--") for a in args):
                rest, script_seen, k = [], any(a in ("-e", "-E") for a in args), 0
                while k < len(args):
                    a = args[k]
                    if a in ("-e", "-E"):
                        k += 2
                        continue
                    if a.startswith("-"):
                        k += 1
                        continue
                    if not script_seen:
                        script_seen = True
                    else:
                        rest.append(a)
                    k += 1
                ops += [(_resolve(a, base), "e") for a in rest if _looks_like_path(a)]
            elif prog in ("cp", "mv", "install") and len([a for a in args if not a.startswith("-")]) >= 2:
                pos = [a for a in args if not a.startswith("-")]
                dest = pos[-1]
                if not _looks_like_path(dest) and dest not in (".", ".."):
                    continue
                dpath = _resolve(dest, base)
                if "-r" in args or "-R" in args or "-a" in args:
                    continue  # recursive copies move directories, not individual files
                for src in pos[:-1]:
                    if not _looks_like_path(src) or "." not in os.path.basename(src.rstrip("/")):
                        continue  # no extension: almost always a directory
                    target = os.path.join(dpath, os.path.basename(src)) if dest.endswith("/") or dest in (".", "..") or os.path.isdir(dpath) else dpath
                    ops.append((target, "w"))
                    if prog == "cp":
                        ops.append((_resolve(src, base), "r"))
            elif prog == "touch":
                ops += [(_resolve(a, base), "w") for a in args if _looks_like_path(a)]
            elif prog in _READERS:
                ops += [(_resolve(a, base), "r") for a in args if _looks_like_path(a)]
            elif prog.startswith("python"):
                code = "\n".join(bodies)
                if "-c" in args and args.index("-c") + 1 < len(args):
                    code += "\n" + args[args.index("-c") + 1]
                if code:
                    # python heuristics can match string literals; when replaying transcripts, keep only real files
                    ops += [o for o in _python_writes(code, base) if not verify_python or os.path.exists(o[0])]
    seen, out = set(), []
    for pth, k in ops:  # one entry per (path, kind) per command
        if "$" in pth or "`" in pth:
            continue
        if (pth, k) not in seen:
            seen.add((pth, k))
            out.append((pth, k))
    return out


def _file_path(inp):
    if not isinstance(inp, dict):
        return ""
    return inp.get("file_path") or inp.get("notebook_path") or ""


# ============================================================== state ======

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.sessions = {}
        self.hosts = {}
        self.approvals = {}
        self.clients = []
        self.scan_offsets = {}
        self.seen_usage = {}  # sid -> set(msg ids)
        self.seen_tools = {}  # sid -> set(tool_use ids) for file tracking dedupe (hook + transcript)
        self.account = _empty_account()
        self.acct_ids = set()
        self.acct_dirty = False
        self.limits = {}      # host key -> latest statusline rate_limits snapshot
        self.limit_hist = {}  # host key -> [[ts, five_hour_pct, seven_day_pct], ...]
        self.sl_sig = {}
        self.board = {"cards": {}, "overrides": {}}
        self._cwd_hint = {}
        self.dirty = False
        self.load()

    # ---------- persistence
    def load(self):
        try:
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.sessions = d.get("sessions", {})
            self.hosts = d.get("hosts", {})
            for sid, s in self.sessions.items():
                self.seen_usage[sid] = set(s.pop("_usage_ids", []))
                self.seen_tools[sid] = set(s.pop("_tool_ids", []))
                if s.get("status") in ("running", "thinking", "awaiting_approval", "subagents"):
                    s["status"] = "idle"
                    s["status_detail"] = "Dashboard restarted — waiting for next event"
                s["pending"] = []
            self.scan_offsets = d.get("scan_offsets", {})
            if d.get("files_v") != 3:
                # file-tracking rules changed: drop file history and re-read recent transcripts once (usage is deduped)
                self.scan_offsets = {}
                for sid, s in self.sessions.items():
                    s["files"] = {}
                    self.seen_tools[sid] = set()
            self.limits = d.get("limits", {})
            b = d.get("board") or {}
            self.board = {"cards": b.get("cards") or {}, "overrides": b.get("overrides") or {}}
            self.limit_hist = d.get("limit_hist", {})
            acct = d.get("account") or {}
            if acct.get("v") == ACCOUNT_VERSION:
                self.account = acct
                self.acct_ids = set(d.get("acct_ids", []))
            else:
                # first run with account-level usage: rebuild every total from the transcripts
                self.scan_offsets = {}
                for sid, s in self.sessions.items():
                    s["usage"] = {"in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0, "ctx": 0, "by_model": {}, "side": 0}
                    self.seen_usage[sid] = set()
        except Exception:
            pass

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            cutoff = time.time() - 14 * 86400
            sess = {}
            for sid, s in self.sessions.items():
                if s.get("last_ts", 0) < cutoff:
                    continue
                c = dict(s)
                c["_usage_ids"] = list(self.seen_usage.get(sid, ()))[-5000:]
                c["_tool_ids"] = list(self.seen_tools.get(sid, ()))[-5000:]
                sess[sid] = c
            data = {"sessions": sess, "hosts": self.hosts, "scan_offsets": self.scan_offsets,
                    "account": self.account, "acct_ids": list(self.acct_ids),
                    "limits": self.limits, "limit_hist": self.limit_hist, "files_v": 3,
                    "board": self.board}
            self.dirty = False
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_PATH)

    # ---------- broadcast
    def broadcast(self, msg):
        dead = []
        for q in self.clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            self.clients.remove(q)

    def public_session(self, s):
        c = {k: v for k, v in s.items() if k != "events"}
        return c

    def push_session(self, s, event=None):
        self.dirty = True
        self.broadcast({"type": "session", "session": self.public_session(s), "event": event})

    # ---------- model helpers
    def host_key(self, h):
        return f"{h.get('user','')}@{h.get('hostname','?')}"

    def touch_host(self, h, ts):
        k = self.host_key(h)
        cur = self.hosts.get(k, {})
        cur.update({k2: v for k2, v in h.items() if v not in (None,)})
        cur["key"] = k
        cur["last_seen"] = max(ts, cur.get("last_seen", 0))
        cur["local"] = h.get("hostname") == local_hostname() and not h.get("ssh")
        self.hosts[k] = cur
        self.broadcast({"type": "host", "host": cur})
        return k

    def get_session(self, sid, host_key=None, cwd=None, ts=None):
        s = self.sessions.get(sid)
        if not s:
            s = {
                "id": sid, "host": host_key, "cwd": cwd or "", "project": os.path.basename(cwd or "") or "~",
                "title": "", "prompt": "", "model": "", "branch": "",
                "status": "idle", "status_detail": "Session started", "status_since": ts or time.time(),
                "started": ts or time.time(), "last_ts": ts or time.time(),
                "current": None, "tools_total": 0, "tool_counts": {}, "errors": 0,
                "subagents": {}, "todos": [], "events": [], "pending": [],
                "usage": {"in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0, "ctx": 0, "by_model": {}, "side": 0},
                "turns": 0, "permission_mode": "", "source": "hook", "transcript": "",
                "question": None, "notification": "", "compactions": 0,
            }
            self.sessions[sid] = s
            self.seen_usage[sid] = set()
        if host_key and (not s.get("host") or s.get("source") == "scan"):
            s["host"] = host_key
        if cwd:
            s["cwd"] = cwd
            s["project"] = os.path.basename(cwd.rstrip("/")) or cwd
        return s

    def set_status(self, s, status, detail, ts):
        if s["status"] != status:
            s["status_since"] = ts
        s["status"] = status
        s["status_detail"] = detail

    def add_usage(self, s, entries):
        seen = self.seen_usage.setdefault(s["id"], set())
        u = s["usage"]
        for e in entries:
            mid = e.get("id")
            if not mid:
                continue
            self.account_add(e, s.get("project") or "~", s.get("host") or "?")
            if mid in seen:
                continue
            seen.add(mid)
            for k in ("in", "out", "cr", "cw"):
                u[k] += int(e.get(k) or 0)
            u["msgs"] += 1
            c = cost_of(e)
            u["cost"] = u.get("cost", 0.0) + c
            m = e.get("model") or "unknown"
            bm = u["by_model"].setdefault(m, {"in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0})
            for k in ("in", "out", "cr", "cw"):
                bm[k] += int(e.get(k) or 0)
            bm["msgs"] += 1
            bm["cost"] = bm.get("cost", 0.0) + c
            if e.get("side"):
                u["side"] += int(e.get("in") or 0) + int(e.get("out") or 0) + int(e.get("cr") or 0) + int(e.get("cw") or 0)
                aid = e.get("agent_id")
                if aid:
                    for sa in s["subagents"].values():
                        if sa.get("agent_id") == aid:
                            sa["tokens"] = sa.get("tokens", 0) + int(e.get("out") or 0) + int(e.get("in") or 0) + int(e.get("cr") or 0) + int(e.get("cw") or 0)
            else:
                u["ctx"] = int(e.get("in") or 0) + int(e.get("cr") or 0) + int(e.get("cw") or 0)
                if e.get("model") and e["model"] != "<synthetic>":
                    s["model"] = e["model"]
            ts = e.get("ts")
            if ts:
                hb = u.setdefault("hourly", {})
                hour = ts[:13]
                hb[hour] = hb.get(hour, 0) + int(e.get("out") or 0) + int(e.get("in") or 0) + int(e.get("cw") or 0)
                if len(hb) > 72:
                    for k in sorted(hb)[:-72]:
                        hb.pop(k, None)

    # ---------- board (task cards + manual column overrides for session cards)
    BOARD_COLS = ("backlog", "running", "needs", "review", "done")

    def _push_board(self):
        self.dirty = True
        self.broadcast({"type": "board", "board": self.board})

    def board_upsert(self, body):
        def clip(v, n):
            return str(v or "")[:n]
        with self.lock:
            cid = clip(body.get("id"), 40) or secrets.token_hex(5)
            cur = self.board["cards"].get(cid, {"id": cid, "created": time.time()})
            for k, n in (("title", 200), ("notes", 8000), ("goal", 80), ("cwd", 500), ("session", 80)):
                if k in body:
                    cur[k] = clip(body[k], n)
            if "col" in body:
                col = body["col"] if body["col"] in self.BOARD_COLS else "backlog"
                if col != cur.get("col"):
                    cur["moved"] = time.time()
                cur["col"] = col
            if "prio" in body:
                try:
                    cur["prio"] = max(0, min(3, int(body["prio"])))
                except Exception:
                    pass
            cur.setdefault("col", "backlog")
            cur.setdefault("title", "Untitled task")
            cur["updated"] = time.time()
            self.board["cards"][cid] = cur
            self._push_board()
            return cur

    def board_delete(self, cid):
        with self.lock:
            ok = self.board["cards"].pop(cid, None) is not None
            if ok:
                self._push_board()
            return ok

    def board_override(self, sid, col):
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                return False
            if col in self.BOARD_COLS:
                # sticks until the session gets a new prompt
                self.board["overrides"][sid] = {"col": col, "turns": s.get("turns", 0), "ts": time.time()}
            else:
                self.board["overrides"].pop(sid, None)
            self._push_board()
            return True

    def account_add(self, e, project, host_key):
        mid = e.get("id")
        if not mid or mid in self.acct_ids:
            return
        self.acct_ids.add(mid)
        a = self.account
        c = cost_of(e)
        tin, tout, tcr, tcw = (int(e.get(k) or 0) for k in ("in", "out", "cr", "cw"))
        tok = tin + tout + tcr + tcw
        ts = e.get("ts") or ""
        day = _local_day(ts) if ts else time.strftime("%Y-%m-%d")
        d = a["daily"].setdefault(day, {"tok": 0, "out": 0, "cost": 0.0, "msgs": 0, "models": {}})
        d["tok"] += tok
        d["out"] += tout
        d["cost"] += c
        d["msgs"] += 1
        mk = e.get("model") or "unknown"
        dm = d["models"].setdefault(mk, {"tok": 0, "cost": 0.0})
        dm["tok"] += tok
        dm["cost"] += c
        mm = a["models"].setdefault(mk, {"in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0, "cost": 0.0})
        for k, v in (("in", tin), ("out", tout), ("cr", tcr), ("cw", tcw)):
            mm[k] += v
        mm["msgs"] += 1
        mm["cost"] += c
        pk = f"{host_key}|{project}"
        pp = a["projects"].setdefault(pk, {"project": project, "host": host_key, "tok": 0, "cost": 0.0, "msgs": 0, "last": ""})
        pp["tok"] += tok
        pp["cost"] += c
        pp["msgs"] += 1
        if ts > pp["last"]:
            pp["last"] = ts
        hh = a["hosts"].setdefault(host_key, {"tok": 0, "cost": 0.0, "msgs": 0})
        hh["tok"] += tok
        hh["cost"] += c
        hh["msgs"] += 1
        if ts:
            a["first"] = min(a["first"] or ts, ts)
            a["last"] = max(a["last"] or ts, ts)
        self.acct_dirty = True
        self.dirty = True

    def account_public(self):
        a = self.account
        days = sorted(a["daily"])[-370:]
        projects = sorted(a["projects"].values(), key=lambda x: -x["cost"])[:100]
        return {"daily": {k: a["daily"][k] for k in days}, "models": a["models"], "projects": projects,
                "hosts": a["hosts"], "first": a["first"], "last": a["last"],
                "tok": sum(x["tok"] for x in a["hosts"].values()), "cost": sum(x["cost"] for x in a["hosts"].values()),
                "msgs": sum(x["msgs"] for x in a["hosts"].values())}

    def ingest_statusline(self, ev):
        p = ev.get("payload") or {}
        h = ev.get("host") or {}
        ts = float(ev.get("ts") or time.time())
        with self.lock:
            hk = self.touch_host(h, ts)
            rl = p.get("rate_limits")
            if isinstance(rl, dict) and rl:
                prev = self.limits.get(hk, {})
                snap = {"rate_limits": rl, "ts": ts, "session": p.get("session_id"), "version": p.get("version", "")}
                self.limits[hk] = snap
                fh = (rl.get("five_hour") or {}).get("used_percentage")
                sd = (rl.get("seven_day") or {}).get("used_percentage")
                hist = self.limit_hist.setdefault(hk, [])
                if not hist or ts - hist[-1][0] >= 60 or hist[-1][1] != fh or hist[-1][2] != sd:
                    hist.append([ts, fh, sd])
                    cutoff = ts - 8 * 86400
                    while hist and hist[0][0] < cutoff:
                        hist.pop(0)
                if prev.get("rate_limits") != rl or ts - prev.get("ts", 0) > 30:
                    self.dirty = True
                    self.broadcast({"type": "limits", "host": hk, "limits": snap, "hist": hist[-1500:]})
            sid = p.get("session_id")
            if not sid:
                return
            cwd = (p.get("workspace") or {}).get("current_dir") or p.get("cwd")
            s = self.sessions.get(sid) or self.get_session(sid, hk, cwd, ts)
            cost = p.get("cost") or {}
            cw = p.get("context_window") or {}
            sig = (cost.get("total_cost_usd"), cost.get("total_lines_added"), cost.get("total_lines_removed"),
                   cw.get("used_percentage"), (p.get("model") or {}).get("display_name"))
            if self.sl_sig.get(sid) == sig:
                return
            self.sl_sig[sid] = sig
            s["cost_usd"] = cost.get("total_cost_usd")
            s["lines_added"] = cost.get("total_lines_added")
            s["lines_removed"] = cost.get("total_lines_removed")
            s["api_ms"] = cost.get("total_api_duration_ms")
            s["ctx_pct"] = cw.get("used_percentage")
            s["ctx_size"] = cw.get("context_window_size")
            s["model_name"] = (p.get("model") or {}).get("display_name") or s.get("model_name")
            if (p.get("model") or {}).get("id"):
                s["model"] = p["model"]["id"]
            s["cc_version"] = p.get("version") or s.get("cc_version")
            self.push_session(s)

    def touch_file(self, s, tool, inp, ts, tool_id=None, agent=None, cwd=None, replay=False):
        if tool == "Bash":
            try:
                ops = shell_file_ops((inp or {}).get("command", ""), cwd or s.get("cwd") or "/", verify_python=replay)
            except Exception:
                ops = []
        else:
            kind, path = FILE_TOOLS.get(tool), _file_path(inp)
            ops = [(path, kind)] if kind and path else []
        if not ops:
            return
        if tool_id:
            seen = self.seen_tools.setdefault(s["id"], set())
            if tool_id in seen:
                return
            seen.add(tool_id)
        for path, kind in ops:
            self._touch_one(s, path, kind, inp if tool != "Bash" else {}, ts, agent, via_shell=tool == "Bash")

    def _touch_one(self, s, path, kind, inp, ts, agent, via_shell=False):
        files = s.setdefault("files", {})
        f = files.get(path)
        if not f:
            if len(files) >= MAX_FILES:  # evict the least recently touched
                old = min(files, key=lambda k: files[k]["last"])
                files.pop(old, None)
            f = files[path] = {"r": 0, "e": 0, "w": 0, "last": 0, "agents": []}
        f[kind] += 1
        f["last"] = max(f["last"], ts)
        if via_shell:
            f["sh"] = f.get("sh", 0) + 1
        if kind == "e":
            old_s, new_s = inp.get("old_string"), inp.get("new_string")
            if isinstance(old_s, str) and isinstance(new_s, str):
                f["add"] = f.get("add", 0) + new_s.count("\n") + 1
                f["del"] = f.get("del", 0) + old_s.count("\n") + 1
        if agent and agent not in f["agents"] and len(f["agents"]) < 8:
            f["agents"].append(agent)

    def project_files(self, cwd):
        out = {}
        with self.lock:
            sessions = [x for x in self.sessions.values() if x.get("cwd") == cwd]
            for x in sessions:
                for path, f in (x.get("files") or {}).items():
                    o = out.setdefault(path, {"r": 0, "e": 0, "w": 0, "last": 0, "add": 0, "del": 0, "sessions": 0})
                    for k in ("r", "e", "w", "add", "del"):
                        o[k] += f.get(k, 0)
                    o["last"] = max(o["last"], f.get("last", 0))
                    o["sessions"] += 1
        return {"cwd": cwd, "sessions": len(sessions), "files": out}

    def add_event(self, s, ev):
        s["events"].append(ev)
        if len(s["events"]) > MAX_EVENTS:
            del s["events"][: len(s["events"]) - MAX_EVENTS]

    def find_subagent(self, s, agent_id=None, agent_type=None, running_only=True):
        if agent_id:
            for sa in s["subagents"].values():
                if sa.get("agent_id") == agent_id:
                    return sa
        cands = [sa for sa in s["subagents"].values()
                 if (not running_only or sa["status"] == "running") and not sa.get("agent_id")
                 and (not agent_type or sa.get("type") == agent_type)]
        cands.sort(key=lambda x: x["started"])
        return cands[0] if cands else None

    def recompute_main_status(self, s, ts):
        if s["pending"]:
            return
        running = [sa for sa in s["subagents"].values() if sa["status"] == "running"]
        if s["status"] == "subagents" and not running:
            self.set_status(s, "thinking", "Subagents finished — continuing", ts)

    # ---------- event ingestion
    def ingest(self, ev):
        p = ev.get("payload") or {}
        h = ev.get("host") or {}
        ts = float(ev.get("ts") or time.time())
        name = p.get("hook_event_name", "")
        sid = p.get("session_id")
        if not sid:
            return
        with self.lock:
            hk = self.touch_host(h, ts)
            s = self.get_session(sid, hk, p.get("cwd"), ts)
            s["source"] = "hook"
            s["last_ts"] = max(s["last_ts"], ts)
            s["transcript"] = p.get("transcript_path") or s.get("transcript", "")
            s["pid"] = ev.get("pid")
            if p.get("permission_mode"):
                s["permission_mode"] = p["permission_mode"]
            if ev.get("usage"):
                self.add_usage(s, ev["usage"])
            agent_id = p.get("agent_id")
            sub = self.find_subagent(s, agent_id) if agent_id else None
            tname = p.get("tool_name", "")
            tinput = p.get("tool_input") or {}
            item = {"t": ts, "kind": name, "tool": tname}

            if name == "SessionStart":
                src = p.get("source", "startup")
                self.set_status(s, "idle", f"Session {src}" + (" — ready for a prompt" if src != "compact" else ""), ts)
                if p.get("model"):
                    s["model"] = p["model"] if isinstance(p["model"], str) else s["model"]
                item["text"] = f"Session {src}"
            elif name == "UserPromptSubmit":
                prompt = p.get("prompt", "")
                s["prompt"] = prompt
                s["turns"] += 1
                if not s["title"] and prompt and not prompt.startswith("<"):
                    s["title"] = short(prompt, 90)
                s["question"] = None
                s["notification"] = ""
                self.set_status(s, "thinking", "Reading your prompt and planning", ts)
                item["text"] = short(prompt, 600)
            elif name == "PreToolUse":
                summ = tool_summary(tname, tinput)
                item["text"] = summ
                item["id"] = p.get("tool_use_id")
                if tname not in ("TodoWrite",):
                    item["inp"] = _input_preview(tinput)
                self.touch_file(s, tname, tinput, ts, p.get("tool_use_id"), sub["key"] if sub else None, p.get("cwd"))
                if tname in ("Task", "Agent"):
                    key = p.get("tool_use_id") or f"t{ts}"
                    s["subagents"][key] = {
                        "parent": sub["key"] if sub else None, "tool_counts": {},
                        "key": key, "type": tinput.get("subagent_type") or "general-purpose",
                        "description": tinput.get("description", ""), "prompt": short(tinput.get("prompt", ""), 1200),
                        "status": "running", "started": ts, "ended": None, "tools": 0, "last_tool": "",
                        "last_detail": "Starting up", "background": bool(tinput.get("run_in_background")),
                        "model": tinput.get("model", ""), "agent_id": None, "tokens": 0, "result": "",
                    }
                if tname == "TodoWrite":
                    s["todos"] = tinput.get("todos") or []
                if sub:
                    sub["tools"] += 1
                    sub["last_tool"] = tname
                    sub["last_detail"] = summ
                    sub.setdefault("tool_counts", {})[tname] = sub.get("tool_counts", {}).get(tname, 0) + 1
                    item["agent"] = sub["key"]
                else:
                    mt = s.setdefault("main_tool_counts", {})
                    mt[tname] = mt.get(tname, 0) + 1
                    s["current"] = {"tool": tname, "summary": summ, "since": ts, "id": p.get("tool_use_id"), "input": _trim_input(tinput)}
                    if tname == "AskUserQuestion":
                        s["question"] = tinput.get("questions")
                        self.set_status(s, "waiting_input", "Asking you a question", ts)
                    elif tname in ("Task", "Agent"):
                        self.set_status(s, "subagents", f"Delegating: {tinput.get('description','')}", ts)
                    else:
                        self.set_status(s, "running", f"{tname}: {summ}", ts)
                s["tools_total"] += 1
                s["tool_counts"][tname] = s["tool_counts"].get(tname, 0) + 1
            elif name in ("PostToolUse", "PostToolUseFailure"):
                failed = name == "PostToolUseFailure" or (isinstance(p.get("tool_response"), dict) and p["tool_response"].get("is_error"))
                item["ok"] = not failed
                item["id"] = p.get("tool_use_id")
                item["text"] = tool_summary(tname, tinput)
                item["result"] = short(p.get("error") or response_brief(p.get("tool_response")), 600)
                if failed:
                    s["errors"] += 1
                # resolve any approval that the terminal already answered
                self._resolve_stale_approvals(s, tname, "answered elsewhere")
                if not tinput and s.get("current") and s["current"].get("id") == p.get("tool_use_id"):
                    item["text"] = s["current"].get("summary", "")
                if tname in ("Task", "Agent"):
                    sa = s["subagents"].get(p.get("tool_use_id"))
                    if sa:
                        item["text"] = f"[{sa['type']}] {sa['description']}"
                    if sa and not sa["background"]:
                        sa["status"] = "failed" if failed else "done"
                        sa["ended"] = ts
                        r = p.get("tool_response")
                        if isinstance(r, dict):
                            sa["tokens"] = r.get("totalTokens") or sa["tokens"]
                            sa["tools"] = r.get("totalToolUseCount") or sa["tools"]
                            cont = r.get("content")
                            if isinstance(cont, list) and cont and isinstance(cont[0], dict):
                                sa["result"] = short(cont[0].get("text", ""), 600)
                if sub:
                    item["agent"] = sub["key"]
                    if failed:
                        sub["last_detail"] = f"{tname} failed"
                else:
                    if s.get("current") and s["current"].get("id") == p.get("tool_use_id"):
                        item["dur"] = ts - s["current"]["since"]
                    s["current"] = None
                    if tname == "AskUserQuestion":
                        s["question"] = None
                    running = [x for x in s["subagents"].values() if x["status"] == "running"]
                    if running and s["status"] != "awaiting_approval":
                        self.set_status(s, "subagents", f"{len(running)} subagent(s) working", ts)
                    elif not s["pending"]:
                        self.set_status(s, "thinking", "Thinking about the result" if not failed else f"{tname} failed — recovering", ts)
            elif name == "SubagentStart":
                sa = self.find_subagent(s, None, p.get("agent_type"))
                if sa:
                    sa["agent_id"] = agent_id
                    sa["last_detail"] = "Working"
                    item["agent"] = sa["key"]
                item["text"] = f"{p.get('agent_type','agent')} started"
            elif name == "SubagentStop":
                sa = sub or self.find_subagent(s, None, p.get("agent_type"))
                if sa:
                    sa["status"] = "done"
                    sa["ended"] = ts
                    sa["last_detail"] = "Finished"
                    if p.get("last_assistant_message"):
                        sa["result"] = short(p["last_assistant_message"], 600)
                    item["agent"] = sa["key"]
                item["text"] = f"{p.get('agent_type') or (sa or {}).get('type','subagent')} finished"
                self.recompute_main_status(s, ts)
            elif name == "Notification":
                msg = p.get("message", "")
                ntype = p.get("notification_type", "")
                s["notification"] = msg
                item["text"] = msg
                if ntype == "permission_prompt" or "permission" in msg.lower():
                    if not s["pending"]:
                        self.set_status(s, "awaiting_approval", msg or "Needs your permission (terminal)", ts)
                elif ntype == "idle_prompt" or "waiting for your input" in msg.lower():
                    self.set_status(s, "waiting_input", msg or "Waiting for your input", ts)
            elif name == "Stop":
                s["current"] = None
                self._resolve_stale_approvals(s, None, "turn ended")
                running = [x for x in s["subagents"].values() if x["status"] == "running"]
                detail = "Turn complete — waiting for your next prompt"
                if running:
                    detail += f" ({len(running)} background agent(s) still running)"
                self.set_status(s, "idle", detail, ts)
                if p.get("last_assistant_message"):
                    s["last_reply"] = short(p["last_assistant_message"], 1500)
                    item["text"] = short(p["last_assistant_message"], 600)
            elif name == "StopFailure":
                s["current"] = None
                self.set_status(s, "error", short(p.get("error") or p.get("message") or "Turn failed (API error)", 200), ts)
                item["text"] = s["status_detail"]
                item["ok"] = False
            elif name == "PreCompact":
                s["compactions"] += 1
                self.set_status(s, "running", "Compacting conversation context", ts)
                item["text"] = f"Context compaction ({p.get('trigger','auto')})"
            elif name == "SessionEnd":
                s["current"] = None
                for sa in s["subagents"].values():
                    if sa["status"] == "running":
                        sa["status"] = "stopped"
                        sa["ended"] = ts
                self._resolve_stale_approvals(s, None, "session ended")
                self.set_status(s, "ended", f"Session ended ({p.get('reason','exit')})", ts)
                item["text"] = s["status_detail"]
            else:
                item["text"] = name

            self.add_event(s, item)
            self.push_session(s, item)

    def _resolve_stale_approvals(self, s, tool, why):
        for aid in list(s["pending"]):
            a = self.approvals.get(aid)
            if a and a["status"] == "pending" and (tool is None or a["tool"] == tool):
                a["status"] = "resolved"
                a["decision"] = {"behavior": "ask"}
                a["note"] = why
                s["pending"].remove(aid)
                self.broadcast({"type": "approval", "approval": a})
        self.cond.notify_all()

    # ---------- approvals
    def create_approval(self, ev, timeout):
        p = ev.get("payload") or {}
        aid = secrets.token_hex(6)
        with self.lock:
            self.ingest_lite(ev)
            sid = p.get("session_id")
            s = self.sessions.get(sid)
            a = {
                "id": aid, "session": sid, "status": "pending", "created": time.time(),
                "expires": time.time() + timeout, "tool": p.get("tool_name", ""),
                "input": _trim_input(p.get("tool_input") or {}, 20000),
                "summary": tool_summary(p.get("tool_name", ""), p.get("tool_input") or {}),
                "suggestions": p.get("permission_suggestions") or [],
                "agent_id": p.get("agent_id"), "decision": None,
                "host": s["host"] if s else "", "project": s["project"] if s else "",
                "cwd": p.get("cwd", ""),
            }
            self.approvals[aid] = a
            if s:
                s["pending"].append(aid)
                self.set_status(s, "awaiting_approval", f"Waiting for approval: {a['tool']} — {short(a['summary'], 100)}", time.time())
                item = {"t": time.time(), "kind": "PermissionRequest", "tool": a["tool"], "text": a["summary"], "approval": aid}
                self.add_event(s, item)
                self.push_session(s, item)
            self.broadcast({"type": "approval", "approval": a})
        return a

    def ingest_lite(self, ev):
        p = ev.get("payload") or {}
        h = ev.get("host") or {}
        ts = float(ev.get("ts") or time.time())
        hk = self.touch_host(h, ts)
        s = self.get_session(p.get("session_id"), hk, p.get("cwd"), ts)
        s["last_ts"] = ts
        s["source"] = "hook"
        if p.get("permission_mode"):
            s["permission_mode"] = p["permission_mode"]

    def decide(self, aid, body):
        with self.lock:
            a = self.approvals.get(aid)
            if not a:
                return None
            if a["status"] != "pending":
                return a
            b = body.get("behavior")
            if b == "always":
                a["decision"] = {"behavior": "allow", "updatedPermissions": a["suggestions"]}
            elif b in ("allow", "deny", "ask"):
                a["decision"] = {"behavior": b, "message": body.get("message", ""), "interrupt": bool(body.get("interrupt"))}
            elif b == "expired":
                a["decision"] = {"behavior": "ask"}
            else:
                return a
            a["status"] = {"always": "allowed", "allow": "allowed", "deny": "denied", "ask": "terminal", "expired": "expired"}[b]
            a["decided"] = time.time()
            s = self.sessions.get(a["session"])
            if s and aid in s["pending"]:
                s["pending"].remove(aid)
                label = {"allowed": "Approved from dashboard", "denied": "Denied from dashboard", "terminal": "Handed back to terminal", "expired": "Approval timed out — terminal prompt"}[a["status"]]
                if b == "always":
                    label = "Always-allowed from dashboard"
                if not s["pending"]:
                    self.set_status(s, "running" if a["status"] == "allowed" else ("awaiting_approval" if a["status"] in ("terminal", "expired") else "thinking"),
                                    f"{label}: {a['tool']}", time.time())
                item = {"t": time.time(), "kind": "Decision", "tool": a["tool"], "text": label, "ok": a["status"] == "allowed"}
                self.add_event(s, item)
                self.push_session(s, item)
            self.broadcast({"type": "approval", "approval": a})
            self.cond.notify_all()
            return a

    def wait_approval(self, aid, timeout):
        end = time.time() + timeout
        with self.lock:
            while True:
                a = self.approvals.get(aid)
                if not a:
                    return {"behavior": "ask"}
                if a["status"] != "pending":
                    return a["decision"] or {"behavior": "ask"}
                rem = end - time.time()
                if rem <= 0:
                    return {"behavior": "pending"}
                self.cond.wait(rem)

    def expire_loop(self):
        while True:
            time.sleep(5)
            now = time.time()
            with self.lock:
                for a in list(self.approvals.values()):
                    if a["status"] == "pending" and now > a["expires"] + 10:
                        self.decide(a["id"], {"behavior": "expired"})
                    if a["status"] != "pending" and now - a.get("decided", a["created"]) > 3600:
                        self.approvals.pop(a["id"], None)

    # ---------- local transcript scanner (history + usage without hooks)
    def scan_local(self):
        cutoff = time.time() - SCAN_DAYS * 86400
        host = {"hostname": local_hostname(), "fqdn": socket.gethostname(), "user": os.environ.get("USER", ""),
                "os": f"{os.uname().sysname} {os.uname().release}", "ssh": False, "ssh_from": ""}
        files = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl"))
        for path in files:
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            sid = os.path.basename(path)[:-6]
            history_only = mt < cutoff and sid not in self.sessions
            subs = glob.glob(os.path.join(os.path.dirname(path), sid, "subagents", "*.jsonl"))
            for f, side in [(path, False)] + [(x, True) for x in subs]:
                self._scan_file(sid, f, side, host, mt, history_only)

    def _scan_file(self, sid, path, side, host, mtime, history_only=False):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        off = self.scan_offsets.get(path, 0)
        if off > size:
            off = 0
        if off == size:
            return
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read(size - off)
        nl = chunk.rfind(b"\n")
        if nl < 0:
            return
        lines = chunk[: nl + 1].splitlines()
        self.scan_offsets[path] = off + nl + 1
        usage = {}
        meta = {}
        for line in lines:
            if history_only and meta.get("cwd") and b'"usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if o.get("cwd") and not meta.get("cwd"):
                meta["cwd"] = o["cwd"]
            if o.get("gitBranch"):
                meta["branch"] = o["gitBranch"]
            if o.get("timestamp"):
                meta["last"] = o["timestamp"]
                meta.setdefault("first", o["timestamp"])
            if t == "summary" and o.get("summary"):
                meta["summary"] = o["summary"]
            if t in ("custom-title", "ai-title") and (o.get("customTitle") or o.get("title")):
                meta["summary"] = o.get("customTitle") or o.get("title")
            msg = o.get("message") or {}
            if t == "user" and not side and not o.get("isMeta") and not meta.get("prompt"):
                c = msg.get("content")
                if isinstance(c, str) and c.strip() and not c.startswith("<"):
                    meta["prompt"] = c
            if t == "assistant" and not history_only and isinstance(msg.get("content"), list):
                for blk in msg["content"]:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use" and (blk.get("name") in FILE_TOOLS or blk.get("name") == "Bash"):
                        meta.setdefault("files", []).append((blk.get("name"), blk.get("input") or {}, _iso(o.get("timestamp")) or mtime, blk.get("id"), o.get("cwd")))
            if t == "assistant" and isinstance(msg.get("usage"), dict) and msg.get("model") != "<synthetic>":
                u = msg["usage"]
                mid = msg.get("id") or o.get("uuid")
                usage[mid] = {"id": mid, "model": msg.get("model", ""), "in": u.get("input_tokens", 0) or 0,
                              "out": u.get("output_tokens", 0) or 0, "cr": u.get("cache_read_input_tokens", 0) or 0,
                              "cw": u.get("cache_creation_input_tokens", 0) or 0,
                              "cw1": (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0,
                              "ts": o.get("timestamp"), "side": side or bool(o.get("isSidechain"))}
        if not usage and not meta:
            return
        with self.lock:
            exists = sid in self.sessions
            if history_only or (not exists and side):
                hk = self.host_key(host)
                if hk not in self.hosts:
                    self.touch_host(host, mtime)
                cwd = meta.get("cwd") or self._cwd_hint.get(sid) or ""
                if cwd and not side:
                    self._cwd_hint[sid] = cwd
                proj = os.path.basename(cwd.rstrip("/")) or _project_from_dir(path)
                for e in usage.values():
                    self.account_add(e, proj, hk)
                return
            s = self.sessions.get(sid)
            if not s:
                hk = self.touch_host(host, mtime)
                s = self.get_session(sid, hk, meta.get("cwd"), mtime)
                s["source"] = "scan"
                s["started"] = _iso(meta.get("first")) or mtime
                s["status"] = "ended" if time.time() - mtime > 1800 else "idle"
                s["status_detail"] = "From transcript history" if s["status"] == "ended" else "Idle (no live hook events yet)"
                s["status_since"] = mtime
            if meta.get("branch"):
                s["branch"] = meta["branch"]
            if meta.get("summary"):
                s["title"] = short(meta["summary"], 90)
            elif meta.get("prompt") and not s["title"]:
                s["title"] = short(meta["prompt"], 90)
            if meta.get("prompt") and not s["prompt"]:
                s["prompt"] = meta["prompt"]
            if s["source"] == "scan":
                s["last_ts"] = max(s["last_ts"], _iso(meta.get("last")) or mtime)
            for tool, inp, fts, tid, lcwd in meta.get("files", []):
                self.touch_file(s, tool, inp, fts, tid, "subagent" if side else None, lcwd, replay=True)
            self.add_usage(s, list(usage.values()))
            self.push_session(s)

    def scan_loop(self):
        while True:
            try:
                self.scan_local()
            except Exception as e:
                print("scan error:", e, file=sys.stderr)
            if self.acct_dirty:
                with self.lock:
                    self.acct_dirty = False
                    self.broadcast({"type": "account", "account": self.account_public()})
            try:
                self.save()
            except Exception as e:
                print("save error:", e, file=sys.stderr)
            time.sleep(8)

    def snapshot(self):
        with self.lock:
            return {
                "sessions": list(self.sessions.values()),
                "hosts": list(self.hosts.values()),
                "approvals": [a for a in self.approvals.values() if a["status"] == "pending" or time.time() - a.get("decided", 0) < 600],
                "account": self.account_public(),
                "limits": self.limits,
                "limit_hist": {k: v[-1500:] for k, v in self.limit_hist.items()},
                "board": self.board,
                "server": {"hostname": local_hostname(), "time": time.time(), "version": __version__},
            }


def _project_from_dir(path):
    # ~/.claude/projects/-Users-me-code-app/<sid>.jsonl -> "app" (best effort; dashes are ambiguous)
    parts = path.split(os.sep)
    try:
        d = parts[parts.index("projects") + 1]
        return d.rstrip("-").split("-")[-1] or d
    except Exception:
        return "~"


def _iso(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _input_preview(inp, limit=900):
    """Compact, readable tool input for the trace detail panel."""
    if not isinstance(inp, dict):
        return ""
    if "command" in inp:
        return ("# " + inp["description"] + "\n" if inp.get("description") else "") + "$ " + str(inp["command"])[:limit]
    parts = []
    for k, v in inp.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v)[:300]
        v = str(v)
        parts.append(f"{k}: {v[:300] + ('…' if len(v) > 300 else '')}")
    out = "\n".join(parts)
    return out[:limit] + ("…" if len(out) > limit else "")


def _trim_input(inp, limit=4000):
    try:
        out = {}
        for k, v in inp.items():
            if isinstance(v, str) and len(v) > limit:
                v = v[:limit] + f"\n… ({len(v) - limit} more chars)"
            out[k] = v
        return out
    except Exception:
        return {}


# ============================================================== http =======

STORE = None
TOKEN = None
APPROVAL_TIMEOUT = 300


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentCockpit/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        t = self.headers.get("X-Cockpit-Token") or (q.get("t") or [""])[0]
        return secrets.compare_digest(t or "", TOKEN)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20 * 1024 * 1024:
            return {}
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                html = f.read().replace(b"__COCKPIT_TOKEN__", TOKEN.encode())
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(200, {"ok": True, "version": __version__})
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if path == "/api/state":
            return self._send(200, STORE.snapshot())
        if path == "/api/stream":
            return self._stream()
        m = re.match(r"^/api/approval/([a-f0-9]+)/wait$", path)
        if m:
            q = urllib.parse.parse_qs(u.query)
            to = min(30, max(1, int((q.get("timeout") or ["25"])[0])))
            return self._send(200, STORE.wait_approval(m.group(1), to))
        if path == "/api/project-files":
            q = urllib.parse.parse_qs(u.query)
            return self._send(200, STORE.project_files((q.get("cwd") or [""])[0]))
        m = re.match(r"^/api/session/([\w-]+)/events$", path)
        if m:
            s = STORE.sessions.get(m.group(1))
            return self._send(200, {"events": (s or {}).get("events", [])})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        body = self._body()
        if path == "/api/event":
            STORE.ingest(body)
            return self._send(200, {"ok": True})
        if path == "/api/board/card":
            return self._send(200, STORE.board_upsert(body))
        m = re.match(r"^/api/board/card/([\w-]+)/delete$", path)
        if m:
            return self._send(200, {"ok": STORE.board_delete(m.group(1))})
        m = re.match(r"^/api/board/session/([\w-]+)$", path)
        if m:
            return self._send(200, {"ok": STORE.board_override(m.group(1), body.get("col"))})
        if path == "/api/shutdown":
            # used by a newer plugin version to replace this server
            self._send(200, {"ok": True})
            threading.Timer(0.2, _shutdown).start()
            return
        if path == "/api/statusline":
            STORE.ingest_statusline(body)
            return self._send(200, {"ok": True})
        if path == "/api/events":
            for ev in body.get("events", []):
                try:
                    STORE.ingest(ev)
                except Exception:
                    pass
            return self._send(200, {"ok": True})
        if path == "/api/approval":
            a = STORE.create_approval(body, APPROVAL_TIMEOUT)
            return self._send(200, {"id": a["id"], "timeout": APPROVAL_TIMEOUT})
        m = re.match(r"^/api/approval/([a-f0-9]+)/decide$", path)
        if m:
            a = STORE.decide(m.group(1), body)
            return self._send(200 if a else 404, a or {"error": "unknown approval"})
        m = re.match(r"^/api/session/([\w-]+)/(dismiss)$", path)
        if m:
            with STORE.lock:
                s = STORE.sessions.pop(m.group(1), None)
                STORE.dirty = True
                STORE.broadcast({"type": "remove", "id": m.group(1)})
            return self._send(200, {"ok": bool(s)})
        self._send(404, {"error": "not found"})

    def _stream(self):
        q = queue.Queue(maxsize=5000)
        with STORE.lock:
            STORE.clients.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    data = json.dumps(msg)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with STORE.lock:
                if q in STORE.clients:
                    STORE.clients.remove(q)


def _shutdown():
    try:
        STORE.dirty = True
        STORE.save()
    finally:
        os._exit(0)


def cmd_serve(args):
    global STORE, TOKEN, APPROVAL_TIMEOUT
    TOKEN = ensure_token()
    APPROVAL_TIMEOUT = args.approval_timeout
    STORE = Store()
    threading.Thread(target=STORE.scan_loop, daemon=True).start()
    threading.Thread(target=STORE.expire_loop, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://localhost:{args.port}/"
    print(f"\n  ◆ Agent Cockpit running at {url}\n    token file: {TOKEN_PATH}\n")
    if args.host not in ("127.0.0.1", "localhost"):
        print("  ! Listening on a non-loopback interface; hook calls require the token.\n")

    def stop(*_):
        try:
            STORE.dirty = True
            STORE.save()
        finally:
            os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


# ============================================================ install ======

ALL_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
              "Notification", "Stop", "SubagentStop", "PreCompact"]
NEWER_EVENTS = [("PermissionRequest", (2, 0, 50)), ("SubagentStart", (2, 0, 50)),
                ("PostToolUseFailure", (2, 0, 50)), ("StopFailure", (2, 1, 50))]


def claude_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
        return tuple(int(x) for x in m.groups()) if m else None
    except Exception:
        return None


def cmd_install(args):
    os.makedirs(BASE, exist_ok=True)
    src = os.path.join(HERE, "hook.py")
    dst = os.path.join(BASE, "hook.py")
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    os.chmod(dst, 0o755)
    token = args.token or ensure_token()
    if args.token:
        with open(TOKEN_PATH, "w") as f:
            f.write(args.token)
        os.chmod(TOKEN_PATH, 0o600)
    cfg = _read_config()
    cfg.update({"server": args.server or f"http://127.0.0.1:{getattr(args, 'port', DEFAULT_PORT)}", "token": token,
                "label": args.label or "", "approval_timeout": args.approval_timeout})
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)

    if _plugin_enabled():
        print("  ! The agent-cockpit Claude Code plugin is enabled; it already provides these hooks.")
        print("    Skipping hooks so events are not reported twice (disable the plugin to use this instead).")
        if not getattr(args, "no_statusline", False):
            args.state = "on"
            cmd_statusline(args)
        return
    ver = claude_version()
    events = list(ALL_EVENTS)
    for ev, minv in NEWER_EVENTS:
        if ver is None or ver >= minv:
            events.append(ev)
    py = shutil.which("python3") or sys.executable
    cmd = f'"{py}" "{dst}"'

    settings = {}
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
        bak = SETTINGS_PATH + time.strftime(".bak-agent-cockpit-%Y%m%d-%H%M%S")
        shutil.copy2(SETTINGS_PATH, bak)
        print(f"  backup: {bak}")
    hooks = settings.setdefault("hooks", {})
    _strip_ours(hooks)
    for ev in events:
        timeout = int(args.approval_timeout) + 60 if ev == "PermissionRequest" else 10
        hooks.setdefault(ev, []).append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    if not getattr(args, "no_statusline", False):
        args.state = "on"
        cmd_statusline(args)
    print(f"  ✓ hooks installed for {len(events)} events (claude {'.'.join(map(str, ver)) if ver else 'version unknown'})")
    print(f"  ✓ reporting to {cfg['server']}" + (f" as '{cfg['label']}'" if cfg["label"] else ""))


def _read_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_config(cfg):
    os.makedirs(BASE, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def cmd_statusline(args):
    """Route Claude Code's statusline through the relay so the dashboard gets account rate limits
    (5-hour / weekly) and per-session cost. Any statusline you already had keeps rendering."""
    os.makedirs(BASE, exist_ok=True)
    dst = os.path.join(BASE, "hook.py")
    src = os.path.join(HERE, "hook.py")
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    settings = {}
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
        shutil.copy2(SETTINGS_PATH, SETTINGS_PATH + time.strftime(".bak-agent-cockpit-%Y%m%d-%H%M%S"))
    cfg = _read_config()
    cur = settings.get("statusLine") or {}
    ours = HOOK_MARK in (cur.get("command") or "")
    if args.state == "off":
        if ours:
            orig = cfg.pop("statusline_original", None)
            cfg.pop("statusline_command", None)
            if orig:
                settings["statusLine"] = orig
            else:
                settings.pop("statusLine", None)
            _write_config(cfg)
            with open(SETTINGS_PATH, "w") as f:
                json.dump(settings, f, indent=2)
        print("  ✓ statusline relay off" + (" (your previous statusline is restored)" if ours and cfg is not None else ""))
        return
    if not ours:
        if cur:
            cfg["statusline_original"] = cur
            if cur.get("type", "command") == "command" and cur.get("command"):
                cfg["statusline_command"] = cur["command"]
        _write_config(cfg)
    py = shutil.which("python3") or sys.executable
    new = dict(cfg.get("statusline_original") or {})
    new.update({"type": "command", "command": f'"{py}" "{dst}" --statusline'})
    new.setdefault("refreshInterval", 60)
    settings["statusLine"] = new
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    print("  ✓ statusline relay on — account limits (5-hour / weekly) and session cost reach the dashboard"
          + (f"\n    your existing statusline still renders: {cfg['statusline_command']}" if cfg.get("statusline_command") else ""))


def _plugin_enabled():
    try:
        with open(SETTINGS_PATH) as f:
            ep = json.load(f).get("enabledPlugins") or {}
        return any(k.split("@")[0] == "agent-cockpit" and v for k, v in ep.items())
    except Exception:
        return False


def _strip_ours(hooks):
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            hs = [h for h in g.get("hooks", []) if HOOK_MARK not in h.get("command", "")]
            if hs:
                g["hooks"] = hs
                groups.append(g)
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]


def cmd_uninstall(args):
    if not os.path.exists(SETTINGS_PATH):
        return print("  nothing to do")
    with open(SETTINGS_PATH) as f:
        settings = json.load(f)
    shutil.copy2(SETTINGS_PATH, SETTINGS_PATH + time.strftime(".bak-agent-cockpit-%Y%m%d-%H%M%S"))
    _strip_ours(settings.get("hooks", {}))
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    print("  ✓ Agent Cockpit hooks removed")
    args.state = "off"
    cmd_statusline(args)
    if getattr(args, "keep_autostart", False) is False:
        try:
            args.state = "off"
            cmd_autostart(args)
        except SystemExit:
            pass


def cmd_connect(args):
    """Copy the hook to a remote box, install it pointing at a reverse tunnel, then ssh in."""
    token = ensure_token()
    port = args.port
    target = args.target
    extra = args.ssh_args
    label = args.label or target.split("@")[-1]
    ssh = ["ssh"] + extra
    print(f"  → preparing {target}")
    rc = subprocess.run(ssh + [target, "mkdir -p ~/.claude/agent-cockpit && command -v python3 >/dev/null || echo NO_PY"],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        sys.exit(f"  ✗ ssh failed: {rc.stderr.strip()}")
    if "NO_PY" in rc.stdout:
        sys.exit("  ✗ python3 not found on remote host — install it first (e.g. sudo apt install python3)")
    scp = ["scp", "-q"] + _scp_args(extra)
    r = subprocess.run(scp + [os.path.join(HERE, "hook.py"), os.path.join(HERE, "cli.py"), f"{target}:.claude/agent-cockpit/"])
    if r.returncode != 0:
        sys.exit("  ✗ scp failed")
    remote_cmd = ("cd ~/.claude/agent-cockpit && python3 cli.py install "
                  f"--server http://127.0.0.1:{port} --token {shlex.quote(token)} --label {shlex.quote(label)} "
                  f"--approval-timeout {args.approval_timeout}")
    subprocess.run(ssh + [target, f"bash -lc {shlex.quote(remote_cmd)}"])
    if args.no_shell:
        print(f"\n  Installed. Open the tunnel yourself with:\n    ssh -R {port}:127.0.0.1:{port} {target}\n")
        return
    print(f"  → opening ssh with reverse tunnel :{port} (Claude Code on {label} now reports here)\n")
    os.execvp("ssh", ["ssh", "-R", f"{port}:127.0.0.1:{port}", "-o", "ServerAliveInterval=30",
                      "-o", "ExitOnForwardFailure=no"] + extra + [target])


def _scp_args(ssh_args):
    out, i = [], 0
    while i < len(ssh_args):
        a = ssh_args[i]
        if a == "-p" and i + 1 < len(ssh_args):
            out += ["-P", ssh_args[i + 1]]; i += 2; continue
        if a in ("-i", "-J", "-o", "-F") and i + 1 < len(ssh_args):
            out += [a, ssh_args[i + 1]]; i += 2; continue
        i += 1
    return out


PLIST = os.path.join(HOME, "Library", "LaunchAgents", "tech.agentcockpit.dashboard.plist")
UNIT = os.path.join(HOME, ".config", "systemd", "user", "agent-cockpit.service")
LOG_PATH = os.path.join(BASE, "server.log")


def _serve_argv(port):
    return [sys.executable, os.path.join(HERE, "cli.py"), "serve", "--port", str(port)]


def cmd_autostart(args):
    port = getattr(args, "port", DEFAULT_PORT)
    if sys.platform == "darwin":
        if args.state == "off":
            if os.path.exists(PLIST):
                subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
                os.remove(PLIST)
                print("  ✓ login item removed")
            return
        os.makedirs(BASE, exist_ok=True)
        argv = "".join(f"<string>{a}</string>" for a in _serve_argv(port))
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>tech.agentcockpit.dashboard</string>
  <key>ProgramArguments</key><array>{argv}</array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{LOG_PATH}</string><key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict></plist>"""
        _stop_background()
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
        subprocess.run(["launchctl", "load", PLIST])
        print(f"  ✓ dashboard starts at login → http://localhost:{port}/  (log: {LOG_PATH})")
        return
    if shutil.which("systemctl"):
        if args.state == "off":
            if os.path.exists(UNIT):
                subprocess.run(["systemctl", "--user", "disable", "--now", "agent-cockpit"], capture_output=True)
                os.remove(UNIT)
                print("  ✓ systemd user unit removed")
            return
        os.makedirs(os.path.dirname(UNIT), exist_ok=True)
        with open(UNIT, "w") as f:
            f.write(f"[Unit]\nDescription=Agent Cockpit dashboard\n\n[Service]\nExecStart={' '.join(shlex.quote(a) for a in _serve_argv(port))}\n"
                    f"Restart=always\n\n[Install]\nWantedBy=default.target\n")
        _stop_background()
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        subprocess.run(["systemctl", "--user", "enable", "--now", "agent-cockpit"])
        print(f"  ✓ systemd user service enabled → http://localhost:{port}/")
        return
    if args.state == "on":
        print("  ! no launchd/systemd found; run `agent-cockpit start` instead")


PID_PATH = os.path.join(BASE, "server.pid")


def _healthy(port, timeout=1.0):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout)
        return True
    except Exception:
        return False


def cmd_start(args):
    if _healthy(args.port):
        print(f"  ✓ already running → http://localhost:{args.port}/")
    else:
        os.makedirs(BASE, exist_ok=True)
        log = open(LOG_PATH, "a")
        p = subprocess.Popen(_serve_argv(args.port), stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        with open(PID_PATH, "w") as f:
            f.write(str(p.pid))
        for _ in range(30):
            if _healthy(args.port, 0.3):
                break
            time.sleep(0.1)
        print(f"  ✓ dashboard running in background → http://localhost:{args.port}/  (log: {LOG_PATH})")
    if getattr(args, "open", False):
        webbrowser.open(f"http://localhost:{args.port}/")


def _stop_background():
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        os.remove(PID_PATH)
        time.sleep(0.4)
        return True
    except Exception:
        return False


def cmd_stop(args):
    print("  ✓ stopped" if _stop_background() else "  nothing started with `agent-cockpit start` (login items: `agent-cockpit autostart off`)")


def cmd_setup(args):
    print("\n  ◆ Agent Cockpit setup\n")
    cmd_install(args)
    args.state = "on"
    if args.no_autostart:
        cmd_start(args)
    else:
        cmd_autostart(args)
        for _ in range(30):
            if _healthy(args.port, 0.3):
                break
            time.sleep(0.1)
        if not _healthy(args.port):
            cmd_start(args)
    url = f"http://localhost:{args.port}/"
    print(f"\n  Done. Open {url}\n  New `claude` sessions report automatically; restart running ones to pick up the hooks.")
    print("  Remote box:  agent-cockpit connect user@host\n")
    if not args.no_open:
        webbrowser.open(url)


def cmd_status(args):
    cfg = {}
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        pass
    print("  config:", json.dumps({k: v for k, v in cfg.items() if k != "token"}))
    srv = cfg.get("server", f"http://127.0.0.1:{DEFAULT_PORT}")
    try:
        urllib.request.urlopen(srv + "/healthz", timeout=2)
        print(f"  ✓ dashboard reachable at {srv}")
    except Exception as e:
        print(f"  ✗ dashboard not reachable at {srv}: {e}")


def main():
    ap = argparse.ArgumentParser(prog="agent-cockpit", description="Agent Cockpit — live dashboard for Claude Code sessions")
    ap.add_argument("--version", action="version", version="agent-cockpit " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def install_opts(p):
        p.add_argument("--server")
        p.add_argument("--token")
        p.add_argument("--label")
        p.add_argument("--approval-timeout", type=int, default=300)
        p.add_argument("--no-statusline", action="store_true", help="don't route the statusline (no account limits)")

    st = sub.add_parser("setup", help="install hooks, start at login, open the dashboard")
    install_opts(st)
    st.add_argument("--port", type=int, default=DEFAULT_PORT)
    st.add_argument("--no-autostart", action="store_true")
    st.add_argument("--no-open", action="store_true")
    s = sub.add_parser("serve", help="run the dashboard in the foreground")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--open", action="store_true")
    s.add_argument("--approval-timeout", type=int, default=300)
    sp = sub.add_parser("start", help="run the dashboard in the background")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--open", action="store_true")
    sub.add_parser("stop", help="stop a dashboard started with `start`")
    i = sub.add_parser("install", help="add hooks to ~/.claude/settings.json")
    install_opts(i)
    u = sub.add_parser("uninstall", help="remove hooks and login item")
    u.add_argument("--keep-autostart", action="store_true")
    c = sub.add_parser("connect", help="install on a remote host and ssh in with a reverse tunnel")
    c.add_argument("target")
    c.add_argument("--port", type=int, default=DEFAULT_PORT)
    c.add_argument("--label")
    c.add_argument("--approval-timeout", type=int, default=300)
    c.add_argument("--no-shell", action="store_true")
    c.add_argument("ssh_args", nargs=argparse.REMAINDER)
    a = sub.add_parser("autostart", help="start the dashboard at login")
    a.add_argument("state", choices=["on", "off"])
    a.add_argument("--port", type=int, default=DEFAULT_PORT)
    sl = sub.add_parser("statusline", help="relay the statusline for account limits (keeps your existing one)")
    sl.add_argument("state", choices=["on", "off"])
    sub.add_parser("status", help="show config and server health")
    args = ap.parse_args()
    {"setup": cmd_setup, "serve": cmd_serve, "start": cmd_start, "stop": cmd_stop, "install": cmd_install,
     "uninstall": cmd_uninstall, "connect": cmd_connect, "autostart": cmd_autostart, "statusline": cmd_statusline, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
