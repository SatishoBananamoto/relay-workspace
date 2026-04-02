"""Check runner. Executes adapter-owned probes before policy gates pass.

Checks are orthogonal probes owned by the adapter. Each adapter registers
check implementations. The runner executes them and produces CheckResults.
A check result is evidence, not automatic authorization.
"""

from __future__ import annotations

from typing import Any, Callable

from .types import (
    ActionSpec,
    CheckKind,
    CheckMode,
    CheckResult,
    CheckSpec,
    Proposal,
    Resolution,
)


# Check implementation: (proposal, resolution) -> (passed, detail)
CheckImpl = Callable[[Proposal, Resolution], tuple[bool, str]]


class CheckRunner:
    """Executes adapter-owned checks and produces CheckResults."""

    def __init__(self) -> None:
        self._impls: dict[str, CheckImpl] = {}

    def register(self, check_id: str, impl: CheckImpl) -> None:
        """Register a check implementation by its spec ID."""
        self._impls[check_id] = impl

    def run_checks(
        self,
        *,
        spec: ActionSpec,
        proposal: Proposal,
        resolution: Resolution,
        required_check_ids: list[str] | None = None,
    ) -> list[CheckResult]:
        """
        Run all required checks for an action spec.

        If required_check_ids is provided, only run those checks.
        Otherwise, run all cheap checks defined by the spec.

        Checks without registered implementations are marked as failed
        with a 'no_implementation' detail — never silently skipped.
        """
        check_ids = required_check_ids or [c.id for c in spec.cheap_checks]
        check_specs = {c.id: c for c in spec.cheap_checks}
        results: list[CheckResult] = []

        for check_id in check_ids:
            check_spec = check_specs.get(check_id)
            if check_spec and check_spec.mode == CheckMode.EXTERNAL:
                results.append(CheckResult(
                    check_id=check_id,
                    passed=False,
                    detail="awaiting_check_evidence",
                ))
                continue

            impl = self._impls.get(check_id)
            if impl is None:
                results.append(CheckResult(
                    check_id=check_id,
                    passed=False,
                    detail="no_implementation_registered",
                ))
                continue

            try:
                passed, detail = impl(proposal, resolution)
                results.append(CheckResult(
                    check_id=check_id,
                    passed=passed,
                    detail=detail,
                ))
            except Exception as exc:
                results.append(CheckResult(
                    check_id=check_id,
                    passed=False,
                    detail=f"check_error:{exc}",
                ))

        return results

    def all_passed(self, results: list[CheckResult]) -> bool:
        """True only if every check passed."""
        return bool(results) and all(r.passed for r in results)
