"""Intent classification — routes free text to registered action types.

Uses intent_patterns from ActionSpec registrations. No LLM calls.
Pattern-based classification with optional arg extraction.

    from harness.intent import IntentClassifier

    classifier = IntentClassifier(RELAY_ADAPTERS)
    result = classifier.classify("Here's the code:\n```python\ndef foo(): pass\n```")
    # result.action_type == "produce_artifact"
    # result.args == {"_artifact_kind": "python_code", ...}
    # result.confidence == 1.0

If no pattern matches, returns the fallback action type (default: "discuss").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .types import ActionSpec


@dataclass
class IntentMatch:
    """Result of intent classification."""
    action_type: str
    args: dict[str, Any]
    confidence: float  # 1.0 = pattern match, 0.0 = fallback
    matched_pattern: str | None = None


class IntentClassifier:
    """Classifies free text into registered action types using intent patterns.

    Each registered ActionSpec can declare intent_patterns (regex) and an
    arg_extractor (callable). The classifier tries patterns in priority order
    (highest blast_radius first — we want to catch dangerous actions before
    benign ones).

    If an arg_extractor is registered, it runs on match to pull structured
    args from the text. Otherwise, only the action_type is returned.
    """

    def __init__(
        self,
        registry: dict[str, ActionSpec],
        *,
        fallback: str = "discuss",
    ) -> None:
        self._fallback = fallback
        # Build sorted list: higher blast radius first, then by pattern count (more specific first)
        self._specs: list[ActionSpec] = sorted(
            (spec for spec in registry.values() if spec.intent_patterns),
            key=lambda s: (-_blast_priority(s.blast_radius.value), -len(s.intent_patterns)),
        )
        # Pre-compile patterns
        self._compiled: dict[str, list[re.Pattern]] = {}
        for spec in self._specs:
            self._compiled[spec.action_type] = [
                re.compile(p, re.IGNORECASE) for p in spec.intent_patterns
            ]

    @property
    def fallback(self) -> str:
        return self._fallback

    @property
    def registered_types(self) -> list[str]:
        """Action types with intent patterns, in classification priority order."""
        return [s.action_type for s in self._specs]

    def classify(self, text: str) -> IntentMatch:
        """Classify free text into an action type.

        Returns IntentMatch with:
        - action_type: matched type or fallback
        - args: extracted args (if arg_extractor defined)
        - confidence: 1.0 for pattern match, 0.0 for fallback
        - matched_pattern: the regex that matched (if any)
        """
        for spec in self._specs:
            for pattern in self._compiled[spec.action_type]:
                if pattern.search(text):
                    args = {}
                    if spec.arg_extractor:
                        args = spec.arg_extractor(text)
                    return IntentMatch(
                        action_type=spec.action_type,
                        args=args,
                        confidence=1.0,
                        matched_pattern=pattern.pattern,
                    )

        return IntentMatch(
            action_type=self._fallback,
            args={},
            confidence=0.0,
        )

    def classify_all(self, text: str) -> list[IntentMatch]:
        """Return all matching action types, not just the first.

        Useful when text contains multiple intents (e.g., an artifact
        that also contains a fix).
        """
        matches: list[IntentMatch] = []
        for spec in self._specs:
            for pattern in self._compiled[spec.action_type]:
                if pattern.search(text):
                    args = {}
                    if spec.arg_extractor:
                        args = spec.arg_extractor(text)
                    matches.append(IntentMatch(
                        action_type=spec.action_type,
                        args=args,
                        confidence=1.0,
                        matched_pattern=pattern.pattern,
                    ))
                    break  # one match per action type

        if not matches:
            matches.append(IntentMatch(
                action_type=self._fallback,
                args={},
                confidence=0.0,
            ))
        return matches


def _blast_priority(blast: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(blast, 0)
