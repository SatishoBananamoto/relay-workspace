"""Tests for the relay mode system."""

from __future__ import annotations

import pytest
from relay_discussion.modes import ModeSpec, MODES, DEFAULT_MODE, get_mode


class TestModeSpec:
    def test_all_five_modes_exist(self):
        assert set(MODES.keys()) == {"discuss", "debate", "build", "interview", "freeform"}

    def test_default_mode(self):
        assert DEFAULT_MODE == "discuss"

    def test_get_mode_returns_correct_spec(self):
        spec = get_mode("build")
        assert spec.name == "build"
        assert spec.left_role == "builder"
        assert spec.right_role == "reviewer"

    def test_get_mode_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown mode"):
            get_mode("nonexistent")

    def test_all_modes_have_required_fields(self):
        for name, spec in MODES.items():
            assert spec.name == name
            assert isinstance(spec.description, str) and spec.description
            assert isinstance(spec.left_role, str) and spec.left_role
            assert isinstance(spec.right_role, str) and spec.right_role

    def test_build_mode_has_builder_reviewer_roles(self):
        spec = get_mode("build")
        assert spec.left_role == "builder"
        assert spec.right_role == "reviewer"

    def test_debate_has_agreement_detection(self):
        assert get_mode("debate").detect_agreement is True

    def test_build_has_review_and_artifacts(self):
        spec = get_mode("build")
        assert spec.highlight_review is True
        assert spec.track_artifacts is True

    def test_instruction_templates_have_placeholder(self):
        for name, spec in MODES.items():
            if spec.left_instruction_template:
                assert "{other}" in spec.left_instruction_template, f"{name} left template missing {{other}}"
            if spec.right_instruction_template:
                assert "{other}" in spec.right_instruction_template, f"{name} right template missing {{other}}"

    def test_freeform_has_empty_templates(self):
        spec = get_mode("freeform")
        assert spec.left_instruction_template == ""
        assert spec.right_instruction_template == ""

    def test_mode_spec_is_frozen(self):
        spec = get_mode("discuss")
        with pytest.raises(AttributeError):
            spec.name = "hacked"
