# Opt in: block "done" while the gate fails

A Claude Code Stop hook runs when the agent tries to finish. [`ruru-stop-hook.sh`](ruru-stop-hook.sh) runs
`ruru check --mode gate` and, when the gate fails, tells the agent why and keeps it working.

Copy the script into the project, for example to `.claude/hooks/ruru-stop-hook.sh`, make it executable, and add
this to `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": ".claude/hooks/ruru-stop-hook.sh", "timeout": 300 }] }
    ]
  }
}
```

ruru must be on `PATH` (`uv tool install ruru-review`). Keep the budget small: the hook runs every time the agent
stops. ruru's own exit code 2 (an environment problem) is let through, so a broken environment never traps the agent
in a loop.
