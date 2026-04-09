"""Relay mode definitions.

Each mode defines how agents interact: their roles, instruction templates,
whether a workspace is required, and engine behavior flags.

The server is the single source of truth — web UI fetches from /modes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModeSpec:
    """Defines a relay interaction mode.

    Note: Workspace creation (sandbox/direct mounts) is now driven by
    user-supplied --workspace flags, not by mode. Build mode still creates
    inbox/outbox scaffolding implicitly — see SessionManager.create_session.
    """
    name: str
    description: str
    left_role: str
    right_role: str
    left_instruction_template: str
    right_instruction_template: str
    detect_agreement: bool = False
    highlight_review: bool = False
    track_artifacts: bool = False


MODES: dict[str, ModeSpec] = {
    "discuss": ModeSpec(
        name="discuss",
        description="Agents discuss the topic directly with each other. You observe and interject.",
        left_role="peer",
        right_role="peer",
        left_instruction_template=(
            "You are in a discussion with {other}. Discuss the topic directly with them. "
            "The moderator (Satisho) may interject with guidance. "
            "Respond to {other}, not to the moderator."
        ),
        right_instruction_template=(
            "You are in a discussion with {other}. Discuss the topic directly with them. "
            "The moderator (Satisho) may interject with guidance. "
            "Respond to {other}, not to the moderator."
        ),
    ),
    "debate": ModeSpec(
        name="debate",
        description="Agents take opposing positions and challenge each other. You moderate.",
        left_role="opponent",
        right_role="opponent",
        left_instruction_template=(
            "You are debating {other} on this topic. Challenge their claims, find weaknesses "
            "in their reasoning, and defend your position. Be rigorous but intellectually honest. "
            "Address {other} directly."
        ),
        right_instruction_template=(
            "You are debating {other} on this topic. Challenge their claims, find weaknesses "
            "in their reasoning, and defend your position. Be rigorous but intellectually honest. "
            "Address {other} directly."
        ),
        detect_agreement=True,
    ),
    "build": ModeSpec(
        name="build",
        description="One agent builds (code, specs, plans), the other reviews.",
        left_role="builder",
        right_role="reviewer",
        left_instruction_template=(
            "You are the builder. Produce concrete artifacts — code, specs, designs, plans. "
            "After {other} reviews your work, iterate based on their feedback. "
            "Be specific and actionable."
        ),
        right_instruction_template=(
            "You are the reviewer. Critically evaluate what {other} produces. "
            "Find bugs, edge cases, missing requirements, architectural issues. "
            "Be thorough and constructive. Suggest specific fixes."
        ),
        highlight_review=True,
        track_artifacts=True,
    ),
    "interview": ModeSpec(
        name="interview",
        description="One agent asks probing questions, the other answers with depth.",
        left_role="interviewer",
        right_role="interviewee",
        left_instruction_template=(
            "You are interviewing {other} about this topic. Ask probing, insightful questions "
            "that reveal deeper understanding. Follow up on vague or incomplete answers. "
            "Do not answer yourself."
        ),
        right_instruction_template=(
            "You are being interviewed by {other}. Answer their questions with depth and "
            "specificity. Use concrete examples. If you are uncertain, say so and reason through it."
        ),
    ),
    "freeform": ModeSpec(
        name="freeform",
        description="No special instructions. Both agents respond to the topic and each other freely.",
        left_role="agent",
        right_role="agent",
        left_instruction_template="",
        right_instruction_template="",
    ),
}

DEFAULT_MODE = "discuss"


def get_mode(name: str) -> ModeSpec:
    """Return ModeSpec by name. Raises ValueError for unknown modes."""
    if name not in MODES:
        raise ValueError(f"Unknown mode: {name!r}. Available: {', '.join(MODES)}")
    return MODES[name]
