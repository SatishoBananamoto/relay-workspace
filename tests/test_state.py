"""Tests for the proposal state machine and lifecycle tracker."""

from __future__ import annotations

import pytest

from harness.state import LifecycleTracker, ProposalLifecycle, ProposalState


NOW = "2026-04-02T12:00:00Z"


class TestProposalLifecycle:

    def test_initial_state(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        assert lc.current_state == ProposalState.PROPOSED
        assert not lc.is_terminal
        assert lc.transitions == []

    def test_valid_transition(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        t = lc.transition(ProposalState.CHECK, reason="cheap_checks", timestamp=NOW)
        assert lc.current_state == ProposalState.CHECK
        assert t.from_state == ProposalState.PROPOSED
        assert t.to_state == ProposalState.CHECK

    def test_invalid_transition_raises(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        with pytest.raises(ValueError, match="Invalid transition"):
            lc.transition(ProposalState.EXECUTED, reason="skip", timestamp=NOW)

    def test_full_happy_path(self):
        """proposed -> check -> allow -> executed -> effects -> obligations_open"""
        lc = ProposalLifecycle(proposal_id="test:1")
        lc.transition(ProposalState.CHECK, reason="cheap_checks", timestamp=NOW)
        lc.transition(ProposalState.ALLOW, reason="checks_passed", timestamp=NOW)
        lc.transition(ProposalState.EXECUTED, reason="adapter_ok", timestamp=NOW)
        lc.transition(ProposalState.EFFECTS_PERSISTED, reason="stored", timestamp=NOW)
        lc.transition(ProposalState.OBLIGATIONS_OPEN, reason="1 obligation", timestamp=NOW)
        assert lc.current_state == ProposalState.OBLIGATIONS_OPEN
        assert not lc.is_terminal

        lc.transition(ProposalState.OBLIGATIONS_SATISFIED, reason="all_clear", timestamp=NOW)
        assert lc.is_terminal

    def test_deny_is_terminal(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        lc.transition(ProposalState.DENIED, reason="unregistered", timestamp=NOW)
        assert lc.is_terminal

    def test_clarify_to_superseded(self):
        """After clarification, the proposal is superseded by a revised one."""
        lc = ProposalLifecycle(proposal_id="test:1")
        lc.transition(ProposalState.CLARIFY, reason="ambiguity", timestamp=NOW)
        lc.transition(ProposalState.SUPERSEDED, reason="revised_by_caller", timestamp=NOW)
        assert lc.current_state == ProposalState.SUPERSEDED
        assert lc.is_terminal

    def test_approve_to_allow(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        lc.transition(ProposalState.APPROVE, reason="high_risk", timestamp=NOW)
        lc.transition(ProposalState.ALLOW, reason="human_approved", timestamp=NOW)
        assert lc.current_state == ProposalState.ALLOW

    def test_approve_to_deny(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        lc.transition(ProposalState.APPROVE, reason="high_risk", timestamp=NOW)
        lc.transition(ProposalState.DENIED, reason="human_rejected", timestamp=NOW)
        assert lc.is_terminal

    def test_audit_trail(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        lc.transition(ProposalState.CHECK, reason="checks", timestamp=NOW)
        lc.transition(ProposalState.ALLOW, reason="passed", timestamp=NOW)

        trail = lc.audit_trail
        assert len(trail) == 2
        assert trail[0]["from"] == "proposed"
        assert trail[0]["to"] == "check"
        assert trail[1]["to"] == "allow"

    def test_metadata_in_transition(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        lc.transition(
            ProposalState.DENIED,
            reason="conflict",
            timestamp=NOW,
            metadata={"conflicting_commitment": "c:123"},
        )
        trail = lc.audit_trail
        assert trail[0]["conflicting_commitment"] == "c:123"

    def test_failed_is_terminal(self):
        lc = ProposalLifecycle(proposal_id="test:1")
        lc.transition(ProposalState.ALLOW, reason="ok", timestamp=NOW)
        lc.transition(ProposalState.FAILED, reason="adapter_error", timestamp=NOW)
        assert lc.is_terminal


class TestLifecycleTracker:

    def test_create_and_get(self):
        tracker = LifecycleTracker()
        lc = tracker.create("p:1")
        assert tracker.get("p:1") is lc

    def test_duplicate_raises(self):
        tracker = LifecycleTracker()
        tracker.create("p:1")
        with pytest.raises(ValueError, match="already exists"):
            tracker.create("p:1")

    def test_get_missing_returns_none(self):
        tracker = LifecycleTracker()
        assert tracker.get("p:missing") is None

    def test_active_and_terminal(self):
        tracker = LifecycleTracker()
        lc1 = tracker.create("p:1")
        lc2 = tracker.create("p:2")
        lc2.transition(ProposalState.DENIED, reason="bad", timestamp=NOW)

        assert len(tracker.all_active()) == 1
        assert len(tracker.all_terminal()) == 1
        assert tracker.all_active()[0].proposal_id == "p:1"
        assert tracker.all_terminal()[0].proposal_id == "p:2"
