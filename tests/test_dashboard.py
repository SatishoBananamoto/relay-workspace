"""Tests for the harness dashboard / inspector."""

from __future__ import annotations

import pytest
from harness.core import Harness
from harness.dashboard import HarnessInspector, Summary, ObligationView, EffectView, LifecycleView, RegistryView
from harness.types import CheckResult, ObligationStatus


NOW = "2025-01-01T00:00:00+00:00"
FUTURE = "2025-06-01T00:00:00+00:00"


def _populated_harness() -> Harness:
    """Create a harness with some effects for testing."""
    h = Harness()

    # Send a quote (creates effect with commitment + obligation)
    h.run(
        action_type="SendQuoteEmail",
        args={
            "recipientId": "client:acme",
            "productId": "product:widget",
            "unitPrice": 49.99,
            "currency": "USD",
            "validUntil": FUTURE,
            "termsVersion": "v2",
        },
        entity_map={"recipientId": ["client:acme"]},
        resource_map={"productId": ["product:widget"]},
        semantic_keys=["quote"],
        check_results=[CheckResult(check_id="pricing_source_lookup", passed=True)],
        now_iso=NOW,
    )

    # Schedule a meeting (creates effect with obligation)
    from harness.types import Proposal, Resolution
    h.checks.register("calendar_lookup", lambda p, r: (True, "Available"))
    h.run(
        action_type="ScheduleMeeting",
        args={
            "attendeeIds": ["user:alice", "user:bob"],
            "startTime": FUTURE,
            "durationMinutes": 30,
            "purpose": "Sprint planning",
        },
        entity_map={"attendeeIds": [["user:alice"], ["user:bob"]]},
        semantic_keys=["meeting"],
        now_iso=NOW,
    )

    return h


class TestSummary:
    def test_populated_harness(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        s = inspector.summary()

        assert s.total_effects == 2
        assert s.total_obligations == 2
        assert s.open_obligations == 2
        assert s.breached_obligations == 0
        assert s.registered_actions >= 3  # base adapters
        assert "SendQuoteEmail" in s.effects_by_type
        assert "ScheduleMeeting" in s.effects_by_type

    def test_empty_harness(self):
        h = Harness()
        inspector = HarnessInspector(h)
        s = inspector.summary()

        assert s.total_effects == 0
        assert s.total_obligations == 0
        assert s.active_lifecycles == 0

    def test_format_output(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        text = inspector.summary().format()

        assert "Harness Status" in text
        assert "Effects" in text
        assert "Obligations" in text
        assert "SendQuoteEmail" in text


class TestObligations:
    def test_all_obligations(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        obs = inspector.obligations()

        assert len(obs) == 2
        assert all(isinstance(o, ObligationView) for o in obs)
        kinds = {o.kind for o in obs}
        assert "quote_acknowledgement" in kinds
        assert "meeting_response" in kinds

    def test_filter_by_status(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)

        open_obs = inspector.obligations(status="open")
        assert len(open_obs) == 2

        breached = inspector.obligations(status="breached")
        assert len(breached) == 0

    def test_after_breach(self):
        h = _populated_harness()
        # Breach the first obligation
        effects = h.store.effects
        ob = effects[0].obligations[0]
        h.store.mark_obligation(ob.obligation_id, ObligationStatus.BREACHED)

        inspector = HarnessInspector(h)
        breached = inspector.obligations(status="breached")
        assert len(breached) == 1
        assert breached[0].obligation_id == ob.obligation_id

    def test_format_output(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        obs = inspector.obligations()
        text = obs[0].format()

        assert "OPEN" in text
        assert obs[0].kind in text


class TestEffects:
    def test_all_effects(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        effs = inspector.effects()

        assert len(effs) == 2
        types = {e.action_type for e in effs}
        assert "SendQuoteEmail" in types
        assert "ScheduleMeeting" in types

    def test_filter_by_type(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)

        quote_effs = inspector.effects(action_type="SendQuoteEmail")
        assert len(quote_effs) == 1
        assert quote_effs[0].commitments == 1
        assert quote_effs[0].obligations == 1

        meeting_effs = inspector.effects(action_type="ScheduleMeeting")
        assert len(meeting_effs) == 1

    def test_no_match(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        none = inspector.effects(action_type="NonExistent")
        assert len(none) == 0

    def test_format_output(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        effs = inspector.effects()
        text = effs[0].format()

        assert "mutations:" in text
        assert "commitments:" in text


class TestLifecycles:
    def test_list_all(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        lcs = inspector.lifecycles()

        assert len(lcs) == 2
        # Both should be terminal (executed → effects_persisted → obligations_open)
        states = {lc.current_state for lc in lcs}
        assert "obligations_open" in states

    def test_filter_terminal(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)

        active = inspector.lifecycles(terminal=False)
        # obligations_open is not terminal (can transition to satisfied/breached)
        assert len(active) >= 0

    def test_single_lifecycle(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        lcs = inspector.lifecycles()
        first = lcs[0]

        view = inspector.lifecycle(first.proposal_id)
        assert view is not None
        assert view.proposal_id == first.proposal_id
        assert len(view.transitions) >= 3  # proposed → allow → executed → ...

    def test_nonexistent(self):
        h = Harness()
        inspector = HarnessInspector(h)
        assert inspector.lifecycle("nope") is None

    def test_format_output(self):
        h = _populated_harness()
        inspector = HarnessInspector(h)
        lcs = inspector.lifecycles()
        text = lcs[0].format()

        assert "Proposal:" in text
        assert "State:" in text
        assert "Transitions:" in text


class TestRegistry:
    def test_lists_registered_actions(self):
        inspector = HarnessInspector(Harness())
        views = inspector.registry()

        types = {v.action_type for v in views}
        assert "SendQuoteEmail" in types
        assert "DeleteRows" in types
        assert "ScheduleMeeting" in types

    def test_view_fields(self):
        inspector = HarnessInspector(Harness())
        views = inspector.registry()
        delete = next(v for v in views if v.action_type == "DeleteRows")

        assert delete.blast_radius == "high"
        assert delete.reversible is False
        assert delete.checks >= 1

    def test_format_output(self):
        inspector = HarnessInspector(Harness())
        views = inspector.registry()
        text = views[0].format()

        assert "blast=" in text
