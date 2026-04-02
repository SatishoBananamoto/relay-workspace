#!/bin/bash
cd ~/relay/workspace
python3 -m relay_discussion.cli new \
  --topic "Intent to action and real-world understanding in LLMs. Prior conclusions: 1) Uncertainty has types - schema is dangerous. 2) Keep multiple interpretations alive. 3) Three-question commit rule. 4) Actions create obligations from action surface. 5) High confidence + slow feedback is the danger zone. 6) Cheap safety checks survive, expensive ones get cut. Continue deeper, push toward buildable architecture." \
  --left-provider cli-claude \
  --right-provider cli-codex \
  --no-limit \
  --build \
  --tui
