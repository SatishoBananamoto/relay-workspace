"""Tests for the intent classification system."""

from __future__ import annotations

import pytest
from harness.intent import IntentClassifier, IntentMatch
from harness.sdk import action, EffectBuilder
from harness.types import ActionSpec, BlastRadius


# ---------------------------------------------------------------------------
# Test registry — isolated from production
# ---------------------------------------------------------------------------

def _make_test_registry() -> dict[str, ActionSpec]:
    """Build a small registry with intent patterns for testing."""
    reg: dict[str, ActionSpec] = {}

    @action(
        "write_code",
        blast_radius="medium",
        intent=[r"```(?:python|typescript|ts)", r"\bdef \w+\("],
        registry=reg,
    )
    def write_code(args, resolution, now_iso, fx):
        pass

    @action(
        "delete_data",
        blast_radius="high",
        reversible=False,
        intent=[r"\bdelete\b.*\btable\b", r"\bdrop\b.*\btable\b"],
        registry=reg,
    )
    def delete_data(args, resolution, now_iso, fx):
        pass

    @action(
        "ask_permission",
        blast_radius="high",
        reversible=False,
        approval="always",
        intent=[r"\bneed permission\b", r"\bgrant access\b"],
        registry=reg,
    )
    def ask_permission(args, resolution, now_iso, fx):
        pass

    @action(
        "send_message",
        blast_radius="low",
        intent=[r"\bsend\b.*\bmessage\b", r"\bemail\b"],
        registry=reg,
    )
    def send_message(args, resolution, now_iso, fx):
        pass

    return reg


# ---------------------------------------------------------------------------
# Basic classification tests
# ---------------------------------------------------------------------------

class TestIntentClassifier:
    def setup_method(self):
        self.reg = _make_test_registry()
        self.classifier = IntentClassifier(self.reg)

    def test_matches_code_pattern(self):
        result = self.classifier.classify("Here's the fix:\n```python\ndef foo(): pass\n```")
        assert result.action_type == "write_code"
        assert result.confidence == 1.0

    def test_matches_delete_pattern(self):
        result = self.classifier.classify("We need to delete rows from the users table")
        assert result.action_type == "delete_data"
        assert result.confidence == 1.0

    def test_matches_permission_pattern(self):
        result = self.classifier.classify("I need permission to modify the config")
        assert result.action_type == "ask_permission"
        assert result.confidence == 1.0

    def test_matches_message_pattern(self):
        result = self.classifier.classify("Let's send a message to the team")
        assert result.action_type == "send_message"
        assert result.confidence == 1.0

    def test_fallback_on_no_match(self):
        result = self.classifier.classify("The architecture looks good overall.")
        assert result.action_type == "discuss"
        assert result.confidence == 0.0
        assert result.matched_pattern is None

    def test_custom_fallback(self):
        classifier = IntentClassifier(self.reg, fallback="unknown")
        result = classifier.classify("Nothing matches here.")
        assert result.action_type == "unknown"

    def test_case_insensitive(self):
        result = self.classifier.classify("I NEED PERMISSION to do this")
        assert result.action_type == "ask_permission"

    def test_matched_pattern_reported(self):
        result = self.classifier.classify("```python\nx = 1\n```")
        assert result.matched_pattern is not None
        assert "python" in result.matched_pattern


# ---------------------------------------------------------------------------
# Priority ordering tests
# ---------------------------------------------------------------------------

class TestPriority:
    def test_high_blast_wins_over_medium(self):
        """When text matches both high and medium blast actions, high wins."""
        reg: dict[str, ActionSpec] = {}

        @action(
            "dangerous_write",
            blast_radius="high",
            intent=[r"\bwrite\b"],
            registry=reg,
        )
        def dangerous(args, res, now, fx):
            pass

        @action(
            "safe_write",
            blast_radius="low",
            intent=[r"\bwrite\b"],
            registry=reg,
        )
        def safe(args, res, now, fx):
            pass

        classifier = IntentClassifier(reg)
        result = classifier.classify("please write the data")
        assert result.action_type == "dangerous_write"

    def test_registered_types_in_priority_order(self):
        reg = _make_test_registry()
        classifier = IntentClassifier(reg)
        types = classifier.registered_types
        # High blast actions should come first
        assert types.index("delete_data") < types.index("write_code")
        assert types.index("ask_permission") < types.index("send_message")


# ---------------------------------------------------------------------------
# classify_all tests
# ---------------------------------------------------------------------------

class TestClassifyAll:
    def test_multiple_matches(self):
        reg = _make_test_registry()
        classifier = IntentClassifier(reg)
        # Text that matches both code and delete
        text = "```python\ndelete rows from the users table\n```"
        results = classifier.classify_all(text)
        types = {r.action_type for r in results}
        assert "write_code" in types
        assert "delete_data" in types

    def test_no_match_returns_fallback(self):
        reg = _make_test_registry()
        classifier = IntentClassifier(reg)
        results = classifier.classify_all("Just a normal discussion.")
        assert len(results) == 1
        assert results[0].action_type == "discuss"
        assert results[0].confidence == 0.0


# ---------------------------------------------------------------------------
# Arg extractor tests
# ---------------------------------------------------------------------------

class TestArgExtractor:
    def test_extractor_called_on_match(self):
        reg: dict[str, ActionSpec] = {}

        @action(
            "extract_test",
            intent=[r"\bfile:\s*(\S+)"],
            registry=reg,
        )
        def extract_test(args, res, now, fx):
            pass

        @extract_test.extract_args
        def extract(text):
            import re
            m = re.search(r"\bfile:\s*(\S+)", text)
            return {"filename": m.group(1)} if m else {}

        classifier = IntentClassifier(reg)
        result = classifier.classify("Please update file: main.py with the fix")
        assert result.action_type == "extract_test"
        assert result.args["filename"] == "main.py"

    def test_no_extractor_returns_empty_args(self):
        reg: dict[str, ActionSpec] = {}

        @action(
            "no_extract",
            intent=[r"\bhello\b"],
            registry=reg,
        )
        def no_extract(args, res, now, fx):
            pass

        classifier = IntentClassifier(reg)
        result = classifier.classify("hello world")
        assert result.args == {}

    def test_empty_registry(self):
        classifier = IntentClassifier({})
        result = classifier.classify("anything")
        assert result.action_type == "discuss"
        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Integration: relay adapter intent patterns
# ---------------------------------------------------------------------------

class TestRelayIntentPatterns:
    """Verify the relay adapter's intent patterns work through the classifier."""

    def setup_method(self):
        from relay_discussion.harness_adapter import RELAY_ADAPTERS
        self.classifier = IntentClassifier(RELAY_ADAPTERS)

    def test_python_code_classified_as_artifact(self):
        text = "Here's the implementation:\n```python\ndef solve(x):\n    return x * 2\n```"
        result = self.classifier.classify(text)
        assert result.action_type == "produce_artifact"

    def test_typescript_code_classified_as_artifact(self):
        text = "```typescript\nconst x: number = 42;\n```"
        result = self.classifier.classify(text)
        assert result.action_type == "produce_artifact"

    def test_permission_request_classified(self):
        text = "I need write permission to modify the shared config."
        result = self.classifier.classify(text)
        assert result.action_type == "request_permission"

    def test_fix_classified(self):
        text = "I'll fix the race condition in the lock manager."
        result = self.classifier.classify(text)
        assert result.action_type == "fix_issue"

    def test_discussion_falls_through(self):
        text = "The architecture has three main layers that we should evaluate."
        result = self.classifier.classify(text)
        assert result.action_type == "discuss"

    def test_analysis_classified(self):
        text = "Based on my review, the build order should be reversed."
        result = self.classifier.classify(text)
        assert result.action_type == "analyze"

    def test_plain_discussion_falls_through(self):
        text = "I think we should consider a different approach to the API design."
        result = self.classifier.classify(text)
        assert result.action_type == "discuss"

    def test_artifact_extractor_detects_kind(self):
        text = "```python\nclass Model:\n    pass\n```"
        result = self.classifier.classify(text)
        assert result.args.get("_artifact_kind") == "python_code"

    def test_permission_higher_priority_than_artifact(self):
        """Permission (high blast) should win over artifact (medium) when both match."""
        text = "I need write permission.\n```python\ndef patch(): pass\n```"
        result = self.classifier.classify(text)
        assert result.action_type == "request_permission"
