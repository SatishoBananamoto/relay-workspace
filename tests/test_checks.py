"""Tests for the check runner framework."""

from __future__ import annotations

import pytest

from harness.checks import CheckRunner
from harness.registry import get_spec
from harness.types import (
    ActionSpec,
    ApprovalPolicy,
    BlastRadius,
    CheckKind,
    CheckMode,
    CheckResult,
    CheckSpec,
    Decision,
    FeedbackLatency,
    Proposal,
    Resolution,
)


NOW = "2026-04-02T12:00:00Z"


def _quote_proposal() -> tuple[Proposal, Resolution]:
    return (
        Proposal(
            proposal_id="test:check",
            action_type="SendQuoteEmail",
            args={
                "recipientId": "client:123",
                "productId": "product:abc",
                "unitPrice": 100,
                "currency": "USD",
                "validUntil": "2026-04-10T00:00:00Z",
                "termsVersion": "v2",
            },
        ),
        Resolution(
            entity_ids=["client:123"],
            resource_keys=["product:abc"],
            semantic_keys=["quote"],
        ),
    )


class TestCheckRunner:

    def test_unregistered_check_fails(self):
        """Checks without implementations must fail, never silently skip."""
        runner = CheckRunner()
        spec = get_spec("SendQuoteEmail")
        proposal, resolution = _quote_proposal()

        results = runner.run_checks(spec=spec, proposal=proposal, resolution=resolution)
        assert len(results) == 1
        assert results[0].check_id == "pricing_source_lookup"
        assert results[0].passed is False
        assert "no_implementation" in results[0].detail

    def test_registered_check_executes(self):
        runner = CheckRunner()
        runner.register(
            "pricing_source_lookup",
            lambda p, r: (True, "Price confirmed at $100"),
        )
        spec = get_spec("SendQuoteEmail")
        proposal, resolution = _quote_proposal()

        results = runner.run_checks(spec=spec, proposal=proposal, resolution=resolution)
        assert len(results) == 1
        assert results[0].passed is True
        assert "Price confirmed" in results[0].detail

    def test_failing_check(self):
        runner = CheckRunner()
        runner.register(
            "pricing_source_lookup",
            lambda p, r: (False, "Price source stale: last updated 48h ago"),
        )
        spec = get_spec("SendQuoteEmail")
        proposal, resolution = _quote_proposal()

        results = runner.run_checks(spec=spec, proposal=proposal, resolution=resolution)
        assert results[0].passed is False
        assert not runner.all_passed(results)

    def test_check_exception_handled(self):
        """Exceptions in check implementations produce failed results, not crashes."""
        runner = CheckRunner()

        def exploding_check(p, r):
            raise RuntimeError("Connection refused")

        runner.register("pricing_source_lookup", exploding_check)
        spec = get_spec("SendQuoteEmail")
        proposal, resolution = _quote_proposal()

        results = runner.run_checks(spec=spec, proposal=proposal, resolution=resolution)
        assert results[0].passed is False
        assert "check_error" in results[0].detail

    def test_all_passed_helper(self):
        runner = CheckRunner()
        assert runner.all_passed([
            CheckResult(check_id="a", passed=True),
            CheckResult(check_id="b", passed=True),
        ])
        assert not runner.all_passed([
            CheckResult(check_id="a", passed=True),
            CheckResult(check_id="b", passed=False),
        ])
        assert not runner.all_passed([])

    def test_selective_check_ids(self):
        """Only run specified checks, not all cheap checks."""
        runner = CheckRunner()
        runner.register("pricing_source_lookup", lambda p, r: (True, "ok"))
        runner.register("extra_check", lambda p, r: (True, "ok"))
        spec = get_spec("SendQuoteEmail")
        proposal, resolution = _quote_proposal()

        results = runner.run_checks(
            spec=spec,
            proposal=proposal,
            resolution=resolution,
            required_check_ids=["extra_check"],
        )
        assert len(results) == 1
        assert results[0].check_id == "extra_check"


class TestCheckMode:

    def _make_spec_with_external_check(self) -> ActionSpec:
        return ActionSpec(
            action_type="TestAction",
            version="1",
            required_args=["arg1"],
            blast_radius=BlastRadius.LOW,
            reversible=True,
            feedback_latency=FeedbackLatency.FAST,
            cheap_checks=[
                CheckSpec(
                    id="local_probe",
                    kind=CheckKind.DRY_RUN,
                    required_for=[Decision.ALLOW],
                    mode=CheckMode.LOCAL,
                ),
                CheckSpec(
                    id="human_evidence",
                    kind=CheckKind.LOOKUP,
                    required_for=[Decision.ALLOW],
                    mode=CheckMode.EXTERNAL,
                ),
            ],
            approval_policy=ApprovalPolicy.NEVER,
            effect_template=lambda a, r, n: ([], [], []),
        )

    def test_external_check_fails_without_evidence(self):
        """External checks cannot be auto-run; they fail with awaiting_check_evidence."""
        runner = CheckRunner()
        runner.register("local_probe", lambda p, r: (True, "ok"))
        spec = self._make_spec_with_external_check()
        proposal, resolution = _quote_proposal()

        results = runner.run_checks(spec=spec, proposal=proposal, resolution=resolution)
        by_id = {r.check_id: r for r in results}
        assert by_id["local_probe"].passed is True
        assert by_id["human_evidence"].passed is False
        assert by_id["human_evidence"].detail == "awaiting_check_evidence"

    def test_external_check_with_precomputed_results_works(self):
        """When caller provides check_results, external mode is irrelevant."""
        from harness.core import Harness

        h = Harness()
        spec = self._make_spec_with_external_check()
        # Pre-computed results bypass the runner entirely — the mode
        # only matters during auto-run.
        precomputed = [
            CheckResult(check_id="local_probe", passed=True),
            CheckResult(check_id="human_evidence", passed=True, detail="human confirmed"),
        ]
        assert all(r.passed for r in precomputed)

    def test_local_check_still_runs_normally(self):
        """Local checks with registered implementations run as before."""
        runner = CheckRunner()
        runner.register("local_probe", lambda p, r: (True, "dry run clean"))
        spec = self._make_spec_with_external_check()
        proposal, resolution = _quote_proposal()

        results = runner.run_checks(
            spec=spec,
            proposal=proposal,
            resolution=resolution,
            required_check_ids=["local_probe"],
        )
        assert len(results) == 1
        assert results[0].passed is True
