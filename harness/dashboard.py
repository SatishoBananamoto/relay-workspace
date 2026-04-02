"""Harness dashboard — inspect effects, obligations, lifecycles, and registry.

Programmatic API + CLI:

    # Programmatic
    from harness.dashboard import HarnessInspector
    inspector = HarnessInspector(harness)
    print(inspector.summary())
    for ob in inspector.obligations(status="open"):
        print(ob)

    # CLI (after a relay session)
    python3 -m harness.dashboard status
    python3 -m harness.dashboard obligations --status open
    python3 -m harness.dashboard effects --type produce_artifact
    python3 -m harness.dashboard lifecycle <proposal_id>
    python3 -m harness.dashboard registry
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from .core import Harness
from .registry import REGISTRY
from .state import ProposalLifecycle, ProposalState
from .store import EffectStore
from .types import ActionSpec, Effect, Obligation, ObligationStatus


# ---------------------------------------------------------------------------
# Inspector — query API
# ---------------------------------------------------------------------------

@dataclass
class Summary:
    total_effects: int
    total_obligations: int
    open_obligations: int
    breached_obligations: int
    satisfied_obligations: int
    active_lifecycles: int
    terminal_lifecycles: int
    registered_actions: int
    effects_by_type: dict[str, int]

    def format(self) -> str:
        lines = [
            "Harness Status",
            "=" * 50,
            f"  Effects .............. {self.total_effects}",
        ]
        for at, count in sorted(self.effects_by_type.items()):
            lines.append(f"    {at}: {count}")
        lines.extend([
            f"  Obligations .......... {self.total_obligations}",
            f"    open: {self.open_obligations}",
            f"    breached: {self.breached_obligations}",
            f"    satisfied: {self.satisfied_obligations}",
            f"  Lifecycles ........... {self.active_lifecycles + self.terminal_lifecycles}",
            f"    active: {self.active_lifecycles}",
            f"    terminal: {self.terminal_lifecycles}",
            f"  Registered actions ... {self.registered_actions}",
        ])
        return "\n".join(lines)


@dataclass
class ObligationView:
    obligation_id: str
    kind: str
    status: str
    due_at: str
    verify_with: str
    failure_mode: str
    entity_ids: list[str]
    source_action_type: str

    def format(self) -> str:
        entities = ", ".join(self.entity_ids) if self.entity_ids else "(none)"
        return (
            f"  [{self.status.upper():9s}] {self.obligation_id}\n"
            f"    kind: {self.kind}  |  action: {self.source_action_type}\n"
            f"    due: {self.due_at}  |  verify: {self.verify_with}\n"
            f"    entities: {entities}\n"
            f"    failure: {self.failure_mode}"
        )


@dataclass
class EffectView:
    action_id: str
    action_type: str
    observed_at: str
    mutations: int
    commitments: int
    obligations: int
    entity_ids: list[str]

    def format(self) -> str:
        entities = ", ".join(self.entity_ids[:3])
        if len(self.entity_ids) > 3:
            entities += f" (+{len(self.entity_ids) - 3})"
        return (
            f"  {self.action_id}\n"
            f"    type: {self.action_type}  |  at: {self.observed_at}\n"
            f"    mutations: {self.mutations}  |  commitments: {self.commitments}  |  obligations: {self.obligations}\n"
            f"    entities: {entities or '(none)'}"
        )


@dataclass
class LifecycleView:
    proposal_id: str
    current_state: str
    is_terminal: bool
    transitions: list[dict[str, Any]]

    def format(self) -> str:
        state_label = f"{self.current_state}" + (" (terminal)" if self.is_terminal else " (active)")
        lines = [
            f"  Proposal: {self.proposal_id}",
            f"  State: {state_label}",
            f"  Transitions:",
        ]
        for t in self.transitions:
            lines.append(f"    {t['from']} -> {t['to']}  ({t['reason']})")
        return "\n".join(lines)


@dataclass
class RegistryView:
    action_type: str
    blast_radius: str
    reversible: bool
    approval: str
    intent_patterns: int
    checks: int

    def format(self) -> str:
        flags = []
        if not self.reversible:
            flags.append("irreversible")
        if self.approval == "always":
            flags.append("always-approve")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        return (
            f"  {self.action_type:30s} blast={self.blast_radius:6s} "
            f"intents={self.intent_patterns}  checks={self.checks}{flag_str}"
        )


class HarnessInspector:
    """Read-only query API over a Harness instance."""

    def __init__(self, harness: Harness) -> None:
        self._harness = harness

    def summary(self) -> Summary:
        effects = self._harness.store.effects
        all_obligations = self._all_obligations(effects)
        effects_by_type: dict[str, int] = {}
        for e in effects:
            effects_by_type[e.action_type] = effects_by_type.get(e.action_type, 0) + 1

        return Summary(
            total_effects=len(effects),
            total_obligations=len(all_obligations),
            open_obligations=sum(1 for o in all_obligations if o.status == ObligationStatus.OPEN),
            breached_obligations=sum(1 for o in all_obligations if o.status == ObligationStatus.BREACHED),
            satisfied_obligations=sum(1 for o in all_obligations if o.status == ObligationStatus.SATISFIED),
            active_lifecycles=len(self._harness.lifecycles.all_active()),
            terminal_lifecycles=len(self._harness.lifecycles.all_terminal()),
            registered_actions=len(REGISTRY),
            effects_by_type=effects_by_type,
        )

    def obligations(self, *, status: str | None = None) -> list[ObligationView]:
        effects = self._harness.store.effects
        views = []
        for effect in effects:
            for ob in effect.obligations:
                if status and ob.status.value != status:
                    continue
                views.append(ObligationView(
                    obligation_id=ob.obligation_id,
                    kind=ob.kind,
                    status=ob.status.value,
                    due_at=ob.due_at,
                    verify_with=ob.verify_with.value,
                    failure_mode=ob.failure_mode,
                    entity_ids=ob.entity_ids,
                    source_action_type=effect.action_type,
                ))
        return views

    def effects(self, *, action_type: str | None = None) -> list[EffectView]:
        result = []
        for e in self._harness.store.effects:
            if action_type and e.action_type != action_type:
                continue
            result.append(EffectView(
                action_id=e.action_id,
                action_type=e.action_type,
                observed_at=e.observed_at,
                mutations=len(e.mutations),
                commitments=len(e.commitments),
                obligations=len(e.obligations),
                entity_ids=e.entity_ids,
            ))
        return result

    def lifecycle(self, proposal_id: str) -> LifecycleView | None:
        lc = self._harness.lifecycles.get(proposal_id)
        if lc is None:
            return None
        return LifecycleView(
            proposal_id=lc.proposal_id,
            current_state=lc.current_state.value,
            is_terminal=lc.is_terminal,
            transitions=lc.audit_trail,
        )

    def lifecycles(self, *, terminal: bool | None = None) -> list[LifecycleView]:
        all_lcs = self._harness.lifecycles.lifecycles
        views = []
        for pid, lc in all_lcs.items():
            if terminal is not None and lc.is_terminal != terminal:
                continue
            views.append(LifecycleView(
                proposal_id=lc.proposal_id,
                current_state=lc.current_state.value,
                is_terminal=lc.is_terminal,
                transitions=lc.audit_trail,
            ))
        return views

    def registry(self) -> list[RegistryView]:
        views = []
        for at, spec in sorted(REGISTRY.items()):
            views.append(RegistryView(
                action_type=at,
                blast_radius=spec.blast_radius.value,
                reversible=spec.reversible,
                approval=spec.approval_policy.value,
                intent_patterns=len(spec.intent_patterns),
                checks=len(spec.cheap_checks),
            ))
        return views

    @staticmethod
    def _all_obligations(effects: list[Effect]) -> list[Obligation]:
        result = []
        for e in effects:
            result.extend(e.obligations)
        return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli_status(harness: Harness) -> None:
    inspector = HarnessInspector(harness)
    print(inspector.summary().format())


def _cli_obligations(harness: Harness, status: str | None) -> None:
    inspector = HarnessInspector(harness)
    views = inspector.obligations(status=status)
    if not views:
        print(f"  No obligations{f' with status={status}' if status else ''}.")
        return
    print(f"Obligations ({len(views)}):")
    for v in views:
        print(v.format())
        print()


def _cli_effects(harness: Harness, action_type: str | None) -> None:
    inspector = HarnessInspector(harness)
    views = inspector.effects(action_type=action_type)
    if not views:
        print(f"  No effects{f' for {action_type}' if action_type else ''}.")
        return
    print(f"Effects ({len(views)}):")
    for v in views:
        print(v.format())
        print()


def _cli_lifecycles(harness: Harness, proposal_id: str | None, terminal: bool | None) -> None:
    inspector = HarnessInspector(harness)
    if proposal_id:
        view = inspector.lifecycle(proposal_id)
        if view is None:
            print(f"  No lifecycle found for proposal: {proposal_id}")
            return
        print(view.format())
    else:
        views = inspector.lifecycles(terminal=terminal)
        if not views:
            print("  No lifecycles found.")
            return
        print(f"Lifecycles ({len(views)}):")
        for v in views:
            print(v.format())
            print()


def _cli_registry(harness: Harness) -> None:
    inspector = HarnessInspector(harness)
    views = inspector.registry()
    if not views:
        print("  No actions registered.")
        return
    print(f"Registered Actions ({len(views)}):")
    print(f"  {'Action':30s} {'Blast':8s} {'Intents':9s} {'Checks':8s} Flags")
    print(f"  {'-'*30} {'-'*8} {'-'*9} {'-'*8} {'-'*20}")
    for v in views:
        print(v.format())


def main() -> None:
    """CLI entry point. Runs the demo scenarios then shows dashboard queries."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="harness.dashboard",
        description="Inspect harness state: effects, obligations, lifecycles, registry.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Summary of harness state")

    ob_parser = sub.add_parser("obligations", help="List obligations")
    ob_parser.add_argument("--status", choices=["open", "breached", "satisfied"])

    eff_parser = sub.add_parser("effects", help="List effects")
    eff_parser.add_argument("--type", dest="action_type")

    lc_parser = sub.add_parser("lifecycles", help="List or inspect lifecycles")
    lc_parser.add_argument("proposal_id", nargs="?")
    lc_parser.add_argument("--terminal", action="store_true", default=None)
    lc_parser.add_argument("--active", action="store_true", default=None)

    sub.add_parser("registry", help="List registered action types")
    sub.add_parser("demo", help="Run demo scenarios then show dashboard")

    args = parser.parse_args()

    if args.command == "demo" or args.command is None:
        # Run demo scenarios to populate state, then show dashboard
        from .cli import main as demo_main
        demo_main()
        print()

        # Re-create harness with demo state
        h = Harness()
        from .cli import demo_send_quote, demo_delete_rows, demo_schedule_meeting
        demo_send_quote(h)
        demo_delete_rows(h)
        demo_schedule_meeting(h)

        print("\n")
        _cli_status(h)
        print()
        _cli_obligations(h, status=None)
        _cli_registry(h)
        return

    # For non-demo commands, create a fresh harness (useful as API example)
    h = Harness()
    if args.command == "status":
        _cli_status(h)
    elif args.command == "obligations":
        _cli_obligations(h, status=args.status)
    elif args.command == "effects":
        _cli_effects(h, action_type=args.action_type)
    elif args.command == "lifecycles":
        terminal = None
        if args.terminal:
            terminal = True
        elif args.active:
            terminal = False
        _cli_lifecycles(h, proposal_id=args.proposal_id, terminal=terminal)
    elif args.command == "registry":
        _cli_registry(h)


if __name__ == "__main__":
    main()
