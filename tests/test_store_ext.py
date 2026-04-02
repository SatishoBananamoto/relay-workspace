"""Tests for store extensions: supersession, corrections, lookups."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from harness.sqlite_store import SqliteEffectStore
from harness.store import InMemoryEffectStore
from harness.types import (
    Commitment,
    Effect,
    Mutation,
    Obligation,
    ObligationStatus,
    VerifyWith,
)


NOW = "2026-04-02T12:00:00Z"


def _close_store(store: object) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _make_effect(action_id: str = "action:1", **kwargs) -> Effect:
    return Effect(
        action_id=action_id,
        action_type=kwargs.get("action_type", "SendQuoteEmail"),
        entity_ids=kwargs.get("entity_ids", ["client:123"]),
        resource_keys=kwargs.get("resource_keys", ["product:abc"]),
        semantic_keys=kwargs.get("semantic_keys", ["quote"]),
        mutations=[Mutation(resource="email", op="send", summary="test")],
        commitments=kwargs.get("commitments", [
            Commitment(
                commitment_id="c:1",
                kind="quote",
                entity_ids=["client:123"],
                resource_keys=["product:abc"],
                semantic_keys=["quote"],
                fields={"unitPrice": 100, "currency": "USD", "termsVersion": "v2"},
                expires_at="2026-04-10T00:00:00Z",
            ),
        ]),
        obligations=kwargs.get("obligations", [
            Obligation(
                obligation_id="o:1",
                kind="quote_acknowledgement",
                entity_ids=["client:123"],
                resource_keys=["product:abc"],
                semantic_keys=["quote"],
                due_at="2026-04-10T00:00:00Z",
                verify_with=VerifyWith.HUMAN,
                failure_mode="No reply",
                source_proposal_id="proposal:1",
            ),
        ]),
        observed_at=NOW,
    )


class TestSupersession:

    def test_supersede_commitment(self):
        store = InMemoryEffectStore()
        store.append(_make_effect())

        found = store.supersede_commitment("c:1", superseded_by="c:2")
        assert found is True

        c = store.get_commitment("c:1")
        assert c.superseded_by == "c:2"

    def test_superseded_commitment_not_returned_in_intersection(self):
        store = InMemoryEffectStore()
        store.append(_make_effect())
        store.supersede_commitment("c:1", superseded_by="c:2")

        commitments, _ = store.query_open_intersection(
            action_type="SendQuoteEmail",
            entity_ids=["client:123"],
            resource_keys=["product:abc"],
            semantic_keys=["quote"],
        )
        assert len(commitments) == 0

    def test_supersede_nonexistent_returns_false(self):
        store = InMemoryEffectStore()
        assert store.supersede_commitment("c:missing", superseded_by="c:2") is False


class TestCorrectionEffects:

    def test_correction_is_append_only(self):
        store = InMemoryEffectStore()
        original = _make_effect(action_id="action:orig")
        store.append(original)

        correction = _make_effect(
            action_id="action:correction",
            commitments=[
                Commitment(
                    commitment_id="c:corrected",
                    kind="quote",
                    entity_ids=["client:123"],
                    resource_keys=["product:abc"],
                    semantic_keys=["quote"],
                    fields={"unitPrice": 95, "currency": "USD", "termsVersion": "v2"},
                    expires_at="2026-04-10T00:00:00Z",
                ),
            ],
        )
        store.append_correction("action:orig", correction)

        # Both effects exist
        assert len(store.effects) == 2
        assert store.get_effect("action:orig") is not None
        assert store.get_effect("action:correction") is not None


class TestLookups:

    def test_get_effect_by_id(self):
        store = InMemoryEffectStore()
        store.append(_make_effect(action_id="action:1"))
        store.append(_make_effect(action_id="action:2"))

        assert store.get_effect("action:1").action_id == "action:1"
        assert store.get_effect("action:2").action_id == "action:2"
        assert store.get_effect("action:missing") is None

    def test_get_commitment_by_id(self):
        store = InMemoryEffectStore()
        store.append(_make_effect())

        c = store.get_commitment("c:1")
        assert c is not None
        assert c.kind == "quote"
        assert store.get_commitment("c:missing") is None

    def test_get_obligations_for_proposal(self):
        store = InMemoryEffectStore()
        store.append(_make_effect())

        obligations = store.get_obligations_for_proposal("proposal:1")
        assert len(obligations) == 1
        assert obligations[0].obligation_id == "o:1"

    def test_expiry_comparison_normalizes_iso_offsets(self):
        store = InMemoryEffectStore()
        store.append(_make_effect())

        commitments, _ = store.query_open_intersection(
            action_type="SendQuoteEmail",
            entity_ids=["client:123"],
            resource_keys=["product:abc"],
            semantic_keys=["quote"],
            now_iso="2026-04-10T01:00:01+01:00",
        )
        assert len(commitments) == 0


@pytest.mark.parametrize(
    "store_factory",
    [InMemoryEffectStore, SqliteEffectStore],
    ids=["memory", "sqlite"],
)
def test_expired_commitments_are_ignored_without_explicit_now(
    store_factory: Callable[[], object],
):
    store = store_factory()
    try:
        store.append(
            _make_effect(
                commitments=[
                    Commitment(
                        commitment_id="c:expired",
                        kind="quote",
                        entity_ids=["client:123"],
                        resource_keys=["product:abc"],
                        semantic_keys=["quote"],
                        fields={"unitPrice": 100, "currency": "USD", "termsVersion": "v2"},
                        expires_at="2000-01-01T00:00:00Z",
                    ),
                ],
                obligations=[],
            )
        )

        commitments, _ = store.query_open_intersection(
            action_type="SendQuoteEmail",
            entity_ids=["client:123"],
            resource_keys=["product:abc"],
            semantic_keys=["quote"],
        )
        assert commitments == []
    finally:
        _close_store(store)
