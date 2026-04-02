"""Configuration loader for ~/.relay/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class RelayDefaults:
    moderator: str = "Satisho"
    turns: int = 20
    left_name: str = "Claude"
    left_provider: str = "cli-claude"
    left_model: str = "opus"
    right_name: str = "Codex"
    right_provider: str = "cli-codex"
    right_model: str = "gpt-5.4"
    claude_effort: str = "max"


def load_config(config_path: Path | None = None) -> RelayDefaults:
    """Load config from TOML file, falling back to defaults for missing fields."""
    path = config_path or (Path.home() / ".relay" / "config.toml")
    if not path.exists():
        return RelayDefaults()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    defaults = raw.get("defaults", {})
    agents = raw.get("agents", {})
    claude = agents.get("claude", {})
    codex = agents.get("codex", {})

    return RelayDefaults(
        moderator=defaults.get("moderator", "Satisho"),
        turns=defaults.get("turns", 20),
        left_name=claude.get("name", "Claude"),
        left_provider=claude.get("provider", "cli-claude"),
        left_model=claude.get("model", "opus"),
        right_name=codex.get("name", "Codex"),
        right_provider=codex.get("provider", "cli-codex"),
        right_model=codex.get("model", "gpt-5.4"),
        claude_effort=claude.get("effort", "max"),
    )
