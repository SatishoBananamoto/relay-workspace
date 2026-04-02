"""Interpreter. Maps structured requests to typed proposals with resolution."""

from __future__ import annotations

import uuid
from typing import Any

from .registry import get_spec
from .types import Blocker, Proposal, Resolution, SelectorCandidates, SelectorSpec


def interpret(
    *,
    action_type: str,
    args: dict[str, Any],
    entity_map: dict[str, SelectorCandidates] | None = None,
    resource_map: dict[str, SelectorCandidates] | None = None,
    semantic_keys: list[str] | None = None,
) -> tuple[Proposal, Resolution]:
    """
    Build a typed proposal and resolution from a structured request.

    entity_map:
        - one-selector -> ["entity:1"] or ["entity:1", "entity:2"]
        - many-selector -> [["entity:1"], ["entity:2"]] where each inner list
          represents candidates for one requested item
    resource_map follows the same convention as entity_map
    semantic_keys: domain tags for effect-store intersection queries

    Returns (Proposal, Resolution) with blockers populated.
    """
    proposal_id = f"proposal:{uuid.uuid4().hex[:12]}"
    blockers: list[Blocker] = []

    # Check if action type exists in registry — unregistered types
    # are also caught by core.py, but the interpreter must not lie
    # about the blocker kind if called standalone.
    spec = get_spec(action_type)
    if spec is None:
        return (
            Proposal(
                proposal_id=proposal_id,
                action_type=action_type,
                args=args,
                blockers=blockers,
            ),
            Resolution(),
        )

    # Resolve entities/resources while preserving slot-level outcomes.
    conflicts: list[str] = []
    entity_ids, entity_slots, entity_conflicts = _resolve_selector_map(
        selector_map=entity_map,
        selector_specs={s.name: s for s in spec.entity_selectors},
        missing_prefix="no_match",
        ambiguous_prefix="ambiguous",
    )
    resource_keys, resource_slots, resource_conflicts = _resolve_selector_map(
        selector_map=resource_map,
        selector_specs={s.name: s for s in spec.resource_selectors},
        missing_prefix="no_resource",
        ambiguous_prefix="ambiguous_resource",
    )
    conflicts.extend(entity_conflicts)
    conflicts.extend(resource_conflicts)

    resolution = Resolution(
        entity_ids=entity_ids,
        resource_keys=resource_keys,
        semantic_keys=semantic_keys or [],
        conflicts=conflicts,
        entity_slots=entity_slots,
        resource_slots=resource_slots,
    )

    proposal = Proposal(
        proposal_id=proposal_id,
        action_type=action_type,
        args=args,
        blockers=blockers,
    )

    return proposal, resolution


def _resolve_selector_map(
    *,
    selector_map: dict[str, SelectorCandidates] | None,
    selector_specs: dict[str, SelectorSpec],
    missing_prefix: str,
    ambiguous_prefix: str,
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    resolved_all: list[str] = []
    resolved_slots: dict[str, list[str]] = {}
    conflicts: list[str] = []

    if not selector_map:
        return resolved_all, resolved_slots, conflicts

    for selector_name, raw_candidates in selector_map.items():
        spec = selector_specs.get(selector_name, SelectorSpec(selector_name))
        resolved_slot: list[str] = []

        if spec.cardinality == "many":
            candidate_groups = _candidate_groups(raw_candidates)
            if not candidate_groups:
                conflicts.append(f"{missing_prefix}:{selector_name}")
            for index, candidates in enumerate(candidate_groups):
                if len(candidates) == 0:
                    conflicts.append(f"{missing_prefix}:{selector_name}[{index}]")
                elif len(candidates) == 1:
                    _append_unique(resolved_slot, candidates[0])
                else:
                    conflicts.append(
                        f"{ambiguous_prefix}:{selector_name}[{index}]:{','.join(candidates)}"
                    )
        else:
            candidates = _flatten_candidates(raw_candidates)
            if len(candidates) == 0:
                conflicts.append(f"{missing_prefix}:{selector_name}")
            elif len(candidates) == 1:
                resolved_slot.append(candidates[0])
            else:
                conflicts.append(
                    f"{ambiguous_prefix}:{selector_name}:{','.join(candidates)}"
                )

        if resolved_slot:
            resolved_slots[selector_name] = resolved_slot
            for resolved in resolved_slot:
                _append_unique(resolved_all, resolved)

    return resolved_all, resolved_slots, conflicts


def _candidate_groups(raw_candidates: SelectorCandidates) -> list[list[str]]:
    groups: list[list[str]] = []
    for candidate in raw_candidates:
        if isinstance(candidate, list):
            groups.append(candidate)
        else:
            groups.append([candidate])
    return groups


def _flatten_candidates(raw_candidates: SelectorCandidates) -> list[str]:
    flattened: list[str] = []
    for group in _candidate_groups(raw_candidates):
        flattened.extend(group)
    return flattened


def _append_unique(values: list[str], candidate: str) -> None:
    if candidate not in values:
        values.append(candidate)
