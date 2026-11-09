---
description: Start the Agent Cockpit dashboard (if needed) and open it in the browser
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/src/agent_cockpit/cli.py" start --open`

Tell the user the dashboard is at http://localhost:7777 in one line. To watch a remote machine, they run this in a terminal (not here, it opens an interactive SSH session):
`python3 "${CLAUDE_PLUGIN_ROOT}/src/agent_cockpit/cli.py" connect user@host`
