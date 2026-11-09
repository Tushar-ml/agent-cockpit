# Agent Cockpit

A live dashboard for every Claude Code session you run, on your laptop and on any machine you SSH into. You can watch what each session is doing, approve or deny tool calls from the browser, follow subagents, and see token usage.

![status](https://img.shields.io/badge/python-3.8%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-brightgreen) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

## What you see

- **Status for every session**: running, thinking, subagents working, needs approval, waiting for you, idle, error or ended, with live timers.
- **Approvals in the browser**: Allow, Always allow (adds the suggested permission rule), Deny with a note to Claude, or hand back to the terminal. Shortcuts are `A`, `Shift+A` and `D`, with desktop notifications and a sound.
- **Current work**: the running tool with the full command or edit diff, your last prompt, Claude's last reply, and the task list.
- **Subagents**: type, task brief, current tool, tool calls, tokens, duration and result.
- **Usage**: input, cache read/write and output tokens, a context-window gauge, tokens per hour over 24 h, and per-model and per-tool breakdowns.
- **Machines**: local and remote hosts grouped, with an SSH badge and the source IP. Sessions from the last 3 days are picked up from local transcripts.

Python 3.8+ standard library only, with no dependencies.

## Install: pick one

### Option A: pip

```bash
git clone https://github.com/Tushar-ml/agent-cockpit.git
cd agent-cockpit
pip install .
cockpit setup
```

`cockpit setup` does three things:

- Adds the hooks to `~/.claude/settings.json` (backing up the file first).
- Starts the dashboard at login (launchd on macOS, a systemd user service on Linux).
- Opens **http://localhost:7777**.

New `claude` sessions report straight away. Restart sessions that were already running.

### Option B: Claude Code plugin marketplace

Inside Claude Code:

```
/plugin marketplace add Tushar-ml/agent-cockpit
/plugin install agent-cockpit@agent-cockpit
```

The plugin brings its own hooks, so it doesn't edit `settings.json`. It also starts the dashboard in the background the first time a local session starts. Open the dashboard with `/agent-cockpit:dashboard`, or go to http://localhost:7777.

Use one option or the other, not both. Otherwise every event is reported twice. `cockpit install` detects the plugin and skips its own hooks.

## Watching a remote machine (VM over SSH)

```bash
cockpit connect ubuntu@my-vm                 # ssh flags go after the target: -i key.pem -p 2222 -J bastion
```

This command:

1. Copies the hook to `~/.claude/agent-cockpit/` on the VM.
2. Installs it there, pointing at `127.0.0.1:7777`, and gives it your token.
3. Opens SSH with a reverse tunnel (`-R 7777:127.0.0.1:7777`).

Claude Code on the VM then reports through the tunnel to the dashboard on your laptop, including approvals. The VM shows up with an `SSH` badge.

- While the tunnel is down, events are queued on the VM and sent once it's back. Approval requests go to the VM's terminal prompt instead.
- For a tunnel that stays up: `autossh -M 0 -N -R 7777:127.0.0.1:7777 ubuntu@my-vm`.
- The VM needs `python3`. Pip isn't needed there.

## Commands

| Command | What it does |
|---|---|
| `cockpit setup` | hooks, start at login, open the dashboard |
| `cockpit start` / `stop` | run or stop the dashboard in the background |
| `cockpit serve [--port N] [--open]` | run in the foreground |
| `cockpit install` / `uninstall` | add or remove hooks in `~/.claude/settings.json` (uninstall also removes the login item) |
| `cockpit connect user@host [ssh args]` | set up a remote machine and SSH in with a tunnel |
| `cockpit autostart on\|off` | start at login (launchd or systemd) |
| `cockpit status` | show config and whether the dashboard is reachable |

`agent-cockpit` is the same command under a longer name. `--approval-timeout SECONDS` (default 300) sets how long an approval waits before going to the terminal prompt.

## How it works

```
claude (any machine) ──hook──▶ hook.py ──HTTP──▶ cockpit server (127.0.0.1:7777) ──SSE──▶ browser
                                  ▲                        │
                                  └── approval decision ◀──┘  (PermissionRequest only)
```

Claude Code runs `hook.py` on 13 lifecycle events: session start and end, prompt, before and after each tool call, permission request, notification, subagent start and stop, stop, and compaction. Each time, the hook:

- forwards the event plus host info and any new token-usage lines from the transcript;
- exits 0 within 1.5 s, so Claude is never slowed or broken.

For **PermissionRequest**, the hook waits for your click and returns the decision to Claude Code. If the dashboard is down, it exits in about 0.1 s and you get the normal terminal prompt.

> While the dashboard is running, a permission request waits in the browser first. The terminal prompt appears only after you click **Answer in terminal** or the timeout passes. If another tool also handles `PermissionRequest`, both run.

## Security

- The server listens on `127.0.0.1` only.
- Every API call needs the token in `~/.claude/agent-cockpit/token` (mode 600). The page gets the token from the HTML served on the same origin, so other websites can't read it.
- `connect` copies the token to the VM so its hooks can authenticate through the tunnel.
- Nothing is sent anywhere else.

## Development

```bash
python3 -m unittest discover -s tests -v     # starts a real server and the hook in a temporary HOME
claude plugin validate .
```

| Path | Role |
|---|---|
| `src/agent_cockpit/cli.py` | server and CLI |
| `src/agent_cockpit/hook.py` | hook relay (runs on every machine) |
| `src/agent_cockpit/web/index.html` | the dashboard UI (single file) |
| `.claude-plugin/`, `hooks/hooks.json`, `commands/` | plugin and marketplace manifests |
| `~/.claude/agent-cockpit/` | token, config, saved state, event queue, transcript offsets |

## License

MIT
