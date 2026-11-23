#!/bin/sh
# A Claude Code Stop hook: keep the agent working while ruru's gate fails.
# Exit 2 blocks the stop and shows stderr to the agent. ruru's own exit 2 (it could not test the project) is let
# through, so a broken environment never traps the agent in a loop.
out=$(ruru check --mode gate --quiet --max-mutants 30 --time-budget 240 2>&1)
if [ $? -eq 1 ]; then
    printf '%s\n' "$out" >&2
    exit 2
fi
exit 0
