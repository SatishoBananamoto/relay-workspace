"""SQLite-backed effect store. Drop-in replacement for InMemoryEffectStore."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .types import (
    Commitment,
    Effect,
    Mutation,
    Obligation,
    ObligationStatus,
    VerifyWith,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    action_id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS effect_keys (
    action_id TEXT NOT NULL,
    key_type TEXT NOT NULL,
    key_value TEXT NOT NULL,
    FOREIGN KEY (action_id) REFERENCES effects(action_id)
);
CREATE INDEX IF NOT EXISTS idx_effect_keys ON effect_keys(key_type, key_value);

CREATE TABLE IF NOT EXISTS mutations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    op TEXT NOT NULL,
    summary TEXT NOT NULL,
    FOREIGN KEY (action_id) REFERENCES effects(action_id)
);

CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    expires_at TEXT,
    superseded_by TEXT,
    FOREIGN KEY (action_id) REFERENCES effects(action_id)
);

CREATE TABLE IF NOT EXISTS commitment_keys (
    commitment_id TEXT NOT NULL,
    key_type TEXT NOT NULL,
    key_value TEXT NOT NULL,
    FOREIGN KEY (commitment_id) REFERENCES commitments(commitment_id)
);
CREATE INDEX IF NOT EXISTS idx_commitment_keys ON commitment_keys(key_type, key_value);

CREATE TABLE IF NOT EXISTS obligations (
    obligation_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    verify_with TEXT NOT NULL,
    failure_mode TEXT NOT NULL,
    source_proposal_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    FOREIGN KEY (action_id) REFERENCES effects(action_id)
);

CREATE TABLE IF NOT EXISTS obligation_keys (
    obligation_id TEXT NOT NULL,
    key_type TEXT NOT NULL,
    key_value TEXT NOT NULL,
    FOREIGN KEY (obligation_id) REFERENCES obligations(obligation_id)
);
CREATE INDEX IF NOT EXISTS idx_obligation_keys ON obligation_keys(key_type, key_value);
"""


class SqliteEffectStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._ensure_schema()

    def close(self) -> None:
        self._conn.close()

    def append(self, effect: Effect) -> None:
        c = self._conn
        c.execute(
            "INSERT INTO effects (action_id, action_type, observed_at) VALUES (?, ?, ?)",
            (effect.action_id, effect.action_type, effect.observed_at),
        )
        for key_type, values in [
            ("entity", effect.entity_ids),
            ("resource", effect.resource_keys),
            ("semantic", effect.semantic_keys),
        ]:
            for v in values:
                c.execute(
                    "INSERT INTO effect_keys (action_id, key_type, key_value) VALUES (?, ?, ?)",
                    (effect.action_id, key_type, v),
                )
        for m in effect.mutations:
            c.execute(
                "INSERT INTO mutations (action_id, resource, op, summary) VALUES (?, ?, ?, ?)",
                (effect.action_id, m.resource, m.op, m.summary),
            )
        for commit in effect.commitments:
            c.execute(
                "INSERT INTO commitments (commitment_id, action_id, kind, fields_json, expires_at, superseded_by) VALUES (?, ?, ?, ?, ?, ?)",
                (commit.commitment_id, effect.action_id, commit.kind,
                 json.dumps(commit.fields), commit.expires_at, commit.superseded_by),
            )
            for key_type, values in [
                ("entity", commit.entity_ids),
                ("resource", commit.resource_keys),
                ("semantic", commit.semantic_keys),
            ]:
                for v in values:
                    c.execute(
                        "INSERT INTO commitment_keys (commitment_id, key_type, key_value) VALUES (?, ?, ?)",
                        (commit.commitment_id, key_type, v),
                    )
        for ob in effect.obligations:
            c.execute(
                "INSERT INTO obligations (obligation_id, action_id, kind, due_at, verify_with, failure_mode, source_proposal_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ob.obligation_id, effect.action_id, ob.kind, ob.due_at,
                 ob.verify_with.value, ob.failure_mode, ob.source_proposal_id, ob.status.value),
            )
            for key_type, values in [
                ("entity", ob.entity_ids),
                ("resource", ob.resource_keys),
                ("semantic", ob.semantic_keys),
            ]:
                for v in values:
                    c.execute(
                        "INSERT INTO obligation_keys (obligation_id, key_type, key_value) VALUES (?, ?, ?)",
                        (ob.obligation_id, key_type, v),
                    )
        c.commit()

    @property
    def effects(self) -> list[Effect]:
        return [self._load_effect(row[0]) for row in
                self._conn.execute("SELECT action_id FROM effects ORDER BY rowid").fetchall()]

    def query_open_intersection(
        self,
        *,
        action_type: str,
        entity_ids: list[str],
        resource_keys: list[str],
        semantic_keys: list[str],
        now_iso: str | None = None,
    ) -> tuple[list[Commitment], list[Obligation]]:
        now = now_iso or datetime.now(timezone.utc).isoformat()
        all_keys = (
            [("entity", v) for v in entity_ids]
            + [("resource", v) for v in resource_keys]
            + [("semantic", v) for v in semantic_keys]
        )
        if not all_keys:
            return [], []

        key_clauses = " OR ".join(
            "(ck.key_type = ? AND ck.key_value = ?)" for _ in all_keys
        )
        key_params: list[str] = []
        for kt, kv in all_keys:
            key_params.extend([kt, kv])

        # Commitments: not superseded, not expired, intersecting keys
        c_sql = f"""
            SELECT DISTINCT c.commitment_id
            FROM commitments c
            JOIN commitment_keys ck ON c.commitment_id = ck.commitment_id
            WHERE c.superseded_by IS NULL
              AND ({key_clauses})
        """
        rows = self._conn.execute(c_sql, key_params).fetchall()
        commitments = []
        for (cid,) in rows:
            commit = self._load_commitment(cid)
            if commit.expires_at and _is_past(commit.expires_at, now):
                continue
            commitments.append(commit)

        # Obligations: open status, intersecting keys
        o_sql = f"""
            SELECT DISTINCT o.obligation_id
            FROM obligations o
            JOIN obligation_keys ok ON o.obligation_id = ok.obligation_id
            WHERE o.status = 'open'
              AND ({key_clauses.replace('ck.', 'ok.')})
        """
        rows = self._conn.execute(o_sql, key_params).fetchall()
        obligations = [self._load_obligation(oid) for (oid,) in rows]

        return commitments, obligations

    def get_all_open_obligations(self) -> list[Obligation]:
        rows = self._conn.execute(
            "SELECT obligation_id FROM obligations WHERE status = 'open'"
        ).fetchall()
        return [self._load_obligation(oid) for (oid,) in rows]

    def mark_obligation(self, obligation_id: str, status: ObligationStatus) -> bool:
        cur = self._conn.execute(
            "UPDATE obligations SET status = ? WHERE obligation_id = ?",
            (status.value, obligation_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def supersede_commitment(self, commitment_id: str, superseded_by: str) -> bool:
        cur = self._conn.execute(
            "UPDATE commitments SET superseded_by = ? WHERE commitment_id = ?",
            (superseded_by, commitment_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def append_correction(self, original_action_id: str, correction: Effect) -> None:
        self.append(correction)

    def get_effect(self, action_id: str) -> Effect | None:
        row = self._conn.execute(
            "SELECT action_id FROM effects WHERE action_id = ?", (action_id,)
        ).fetchone()
        return self._load_effect(row[0]) if row else None

    def get_commitment(self, commitment_id: str) -> Commitment | None:
        row = self._conn.execute(
            "SELECT commitment_id FROM commitments WHERE commitment_id = ?",
            (commitment_id,),
        ).fetchone()
        return self._load_commitment(row[0]) if row else None

    def get_obligation(self, obligation_id: str) -> Obligation | None:
        row = self._conn.execute(
            "SELECT obligation_id FROM obligations WHERE obligation_id = ?",
            (obligation_id,),
        ).fetchone()
        return self._load_obligation(row[0]) if row else None

    def get_obligations_for_proposal(self, proposal_id: str) -> list[Obligation]:
        rows = self._conn.execute(
            "SELECT obligation_id FROM obligations WHERE source_proposal_id = ?",
            (proposal_id,),
        ).fetchall()
        return [self._load_obligation(oid) for (oid,) in rows]

    # -- Internal loaders --

    def _load_effect(self, action_id: str) -> Effect:
        row = self._conn.execute(
            "SELECT action_type, observed_at FROM effects WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        action_type, observed_at = row

        entity_ids = self._load_keys("effect_keys", "action_id", action_id, "entity")
        resource_keys = self._load_keys("effect_keys", "action_id", action_id, "resource")
        semantic_keys = self._load_keys("effect_keys", "action_id", action_id, "semantic")

        mutations = [
            Mutation(resource=r[0], op=r[1], summary=r[2])
            for r in self._conn.execute(
                "SELECT resource, op, summary FROM mutations WHERE action_id = ? ORDER BY id",
                (action_id,),
            ).fetchall()
        ]

        commitment_ids = [
            r[0] for r in self._conn.execute(
                "SELECT commitment_id FROM commitments WHERE action_id = ?", (action_id,)
            ).fetchall()
        ]
        commitments = [self._load_commitment(cid) for cid in commitment_ids]

        obligation_ids = [
            r[0] for r in self._conn.execute(
                "SELECT obligation_id FROM obligations WHERE action_id = ?", (action_id,)
            ).fetchall()
        ]
        obligations = [self._load_obligation(oid) for oid in obligation_ids]

        return Effect(
            action_id=action_id,
            action_type=action_type,
            entity_ids=entity_ids,
            resource_keys=resource_keys,
            semantic_keys=semantic_keys,
            mutations=mutations,
            commitments=commitments,
            obligations=obligations,
            observed_at=observed_at,
        )

    def _load_commitment(self, commitment_id: str) -> Commitment:
        row = self._conn.execute(
            "SELECT kind, fields_json, expires_at, superseded_by FROM commitments WHERE commitment_id = ?",
            (commitment_id,),
        ).fetchone()
        kind, fields_json, expires_at, superseded_by = row
        return Commitment(
            commitment_id=commitment_id,
            kind=kind,
            entity_ids=self._load_keys("commitment_keys", "commitment_id", commitment_id, "entity"),
            resource_keys=self._load_keys("commitment_keys", "commitment_id", commitment_id, "resource"),
            semantic_keys=self._load_keys("commitment_keys", "commitment_id", commitment_id, "semantic"),
            fields=json.loads(fields_json),
            expires_at=expires_at,
            superseded_by=superseded_by,
        )

    def _load_obligation(self, obligation_id: str) -> Obligation:
        row = self._conn.execute(
            "SELECT kind, due_at, verify_with, failure_mode, source_proposal_id, status FROM obligations WHERE obligation_id = ?",
            (obligation_id,),
        ).fetchone()
        kind, due_at, verify_with, failure_mode, source_proposal_id, status = row
        return Obligation(
            obligation_id=obligation_id,
            kind=kind,
            entity_ids=self._load_keys("obligation_keys", "obligation_id", obligation_id, "entity"),
            resource_keys=self._load_keys("obligation_keys", "obligation_id", obligation_id, "resource"),
            semantic_keys=self._load_keys("obligation_keys", "obligation_id", obligation_id, "semantic"),
            due_at=due_at,
            verify_with=VerifyWith(verify_with),
            failure_mode=failure_mode,
            source_proposal_id=source_proposal_id,
            status=ObligationStatus(status),
        )

    def _load_keys(self, table: str, id_col: str, id_val: str, key_type: str) -> list[str]:
        return [
            r[0] for r in self._conn.execute(
                f"SELECT key_value FROM {table} WHERE {id_col} = ? AND key_type = ?",
                (id_val, key_type),
            ).fetchall()
        ]

    def _ensure_schema(self) -> None:
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(obligations)").fetchall()
        }
        if "source_proposal_id" not in columns:
            self._conn.execute(
                "ALTER TABLE obligations ADD COLUMN source_proposal_id TEXT"
            )
            self._conn.commit()


def _is_past(timestamp: str, now_iso: str) -> bool:
    return _parse_iso8601(timestamp) < _parse_iso8601(now_iso)


def _parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
