#!/bin/bash
cd ~/relay/workspace
python3 -m relay_discussion.cli new \
  --topic "Build the intent-to-action harness. Claude builds, Codex reviews. Go." \
  --left-provider cli-claude \
  --right-provider cli-codex \
  --turns 4 \
  --build
