#!/bin/bash
cd ~/relay/workspace

# Copy prior artifacts so agents can see them
PREV_WS="$HOME/.relay/sessions/57847688-2f65-4ebf-8e3d-0e4454db523c/workspace"
PRIOR="$HOME/relay/workspace/prior_work"
rm -rf "$PRIOR"
mkdir -p "$PRIOR"
cp -r "$PREV_WS/shared/"* "$PRIOR/" 2>/dev/null
cp -r "$PREV_WS/prototype" "$PRIOR/" 2>/dev/null
echo "Prior artifacts staged."

python3 -m relay_discussion.cli new \
  --topic "Build the intent-to-action harness. Prior work at ~/relay/workspace/prior_work/. Read it first. Architecture: Interpreter, Action Registry, Policy Engine, Executor, Obligation Engine. Claude builds, Codex reviews. Full permissions - read, write, run anything. Go." \
  --left-provider cli-claude \
  --right-provider cli-codex \
  --no-limit \
  --build
