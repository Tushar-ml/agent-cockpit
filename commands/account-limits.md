---
description: Show your plan limits (5-hour / weekly) in Agent Cockpit by relaying the status line
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/src/agent_cockpit/cli.py" statusline on`

Tell the user in two lines: plan limits and session cost now flow to the Account usage page at http://localhost:7777/#account after the next response, and any status line they already had keeps rendering. To undo: `python3 "${CLAUDE_PLUGIN_ROOT}/src/agent_cockpit/cli.py" statusline off`.
