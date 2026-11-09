"""End-to-end smoke test: real server + real hook relay in an isolated HOME.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "src", "agent_cockpit")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls.port = free_port()
        cls.env = dict(os.environ, HOME=cls.home, COCKPIT_URL=f"http://127.0.0.1:{cls.port}", COCKPIT_NO_AUTOSTART="1")
        cls.env.pop("SSH_CONNECTION", None)
        cls.srv = subprocess.Popen([sys.executable, os.path.join(PKG, "cli.py"), "serve", "--port", str(cls.port),
                                    "--approval-timeout", "20"], env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/healthz", timeout=0.3)
                break
            except Exception:
                time.sleep(0.1)
        with open(os.path.join(cls.home, ".claude", "agent-cockpit", "token")) as f:
            cls.token = f.read().strip()

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate()
        cls.srv.wait(5)

    def hook(self, payload, env=None):
        r = subprocess.run([sys.executable, os.path.join(PKG, "hook.py")], input=json.dumps(payload),
                           capture_output=True, text=True, env=dict(self.env, **(env or {})), timeout=60)
        self.assertEqual(r.returncode, 0)
        return r.stdout

    def api(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("X-Cockpit-Token", self.token)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def session(self, sid):
        return next(s for s in self.api("GET", "/api/state")["sessions"] if s["id"] == sid)

    def test_rejects_bad_token(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/state")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=3)

    def test_lifecycle_subagents_usage(self):
        tr = os.path.join(self.home, "t.jsonl")
        with open(tr, "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {
                "id": "m1", "model": "claude-opus-5-5",
                "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40}}}) + "\n")
        b = {"session_id": "s1", "cwd": "/tmp/proj", "transcript_path": tr}
        remote = {"SSH_CONNECTION": "10.1.1.1 1 10.1.1.2 22", "COCKPIT_LABEL": "vm1"}
        self.hook({**b, "hook_event_name": "SessionStart", "source": "startup"}, remote)
        self.hook({**b, "hook_event_name": "UserPromptSubmit", "prompt": "do the thing"}, remote)
        self.hook({**b, "hook_event_name": "PreToolUse", "tool_name": "Task", "tool_use_id": "t1",
                   "tool_input": {"subagent_type": "Explore", "description": "look around", "prompt": "p"}}, remote)
        s = self.session("s1")
        self.assertEqual(s["status"], "subagents")
        self.assertEqual(s["usage"]["out"], 20)
        self.assertEqual(s["usage"]["ctx"], 80)
        self.hook({**b, "hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": "Explore"}, remote)
        self.hook({**b, "hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "Explore"}, remote)
        self.hook({**b, "hook_event_name": "Stop"}, remote)
        s = self.session("s1")
        self.assertEqual(s["status"], "idle")
        self.assertEqual(s["subagents"]["t1"]["status"], "done")
        host = next(h for h in self.api("GET", "/api/state")["hosts"] if h["key"] == s["host"])
        self.assertTrue(host["ssh"])
        self.assertEqual(host["hostname"], "vm1")

    def test_approval_allow_always(self):
        out = {}
        payload = {"session_id": "s2", "cwd": "/tmp/p2", "hook_event_name": "PermissionRequest", "tool_name": "Bash",
                   "tool_input": {"command": "make deploy"},
                   "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "make:*"}],
                                               "behavior": "allow", "destination": "localSettings"}]}
        t = threading.Thread(target=lambda: out.setdefault("v", self.hook(payload)))
        t.start()
        aid = None
        for _ in range(50):
            pend = [a for a in self.api("GET", "/api/state")["approvals"] if a["session"] == "s2" and a["status"] == "pending"]
            if pend:
                aid = pend[0]["id"]
                break
            time.sleep(0.1)
        self.assertIsNotNone(aid)
        self.api("POST", f"/api/approval/{aid}/decide", {"behavior": "always"})
        t.join(15)
        dec = json.loads(out["v"])["hookSpecificOutput"]["decision"]
        self.assertEqual(dec["behavior"], "allow")
        self.assertEqual(dec["updatedPermissions"][0]["rules"][0]["ruleContent"], "make:*")

    def test_approval_deny_with_message(self):
        out = {}
        payload = {"session_id": "s3", "cwd": "/tmp/p3", "hook_event_name": "PermissionRequest", "tool_name": "Write",
                   "tool_input": {"file_path": "/etc/hosts", "content": "x"}}
        t = threading.Thread(target=lambda: out.setdefault("v", self.hook(payload)))
        t.start()
        aid = None
        for _ in range(50):
            pend = [a for a in self.api("GET", "/api/state")["approvals"] if a["session"] == "s3" and a["status"] == "pending"]
            if pend:
                aid = pend[0]["id"]
                break
            time.sleep(0.1)
        self.api("POST", f"/api/approval/{aid}/decide", {"behavior": "deny", "message": "not that file"})
        t.join(15)
        dec = json.loads(out["v"])["hookSpecificOutput"]["decision"]
        self.assertEqual(dec, {"behavior": "deny", "message": "not that file"})

    def test_offline_falls_through_fast(self):
        t0 = time.time()
        out = self.hook({"session_id": "s4", "hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {}},
                        {"COCKPIT_URL": f"http://127.0.0.1:{free_port()}"})
        self.assertEqual(out, "")
        self.assertLess(time.time() - t0, 3)


if __name__ == "__main__":
    unittest.main()
