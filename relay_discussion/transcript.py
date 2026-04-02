from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

from .models import (
    Message,
    compute_conversation_digest,
    compute_resume_state_digest,
    is_valid_fault_state_snapshot,
    is_valid_policy_state_snapshot,
    is_strict_int,
    is_valid_session_snapshot,
)


class TranscriptStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, message: Message) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.to_dict(), ensure_ascii=True))
            handle.write("\n")

    def write(self, messages: Iterable[Message]) -> None:
        with self.path.open("w", encoding="utf-8"):
            pass
        for message in messages:
            self.append(message)

    def read(self) -> list[dict]:
        return [item for _, item in self._iter_dicts()]

    def load_messages(self) -> list[Message]:
        messages: list[Message] = []
        turn_progression: dict[str, object] = {"current_turn": 1, "stage": "interjections"}
        for line_number, item in self._iter_dicts():
            try:
                message = Message(**item)
            except TypeError as exc:
                raise ValueError(
                    f"Transcript contains an invalid message shape at line {line_number}: {self.path}"
                ) from exc
            self._validate_message(line_number=line_number, message=message)
            self._validate_protocol_message(line_number=line_number, message=message)
            self._validate_topic_invariants(line_number=line_number, message=message, prior_messages=messages)
            self._validate_session_consistency(line_number=line_number, message=message, prior_messages=messages)
            if messages and message.seq <= messages[-1].seq:
                raise ValueError(
                    f"Transcript contains a non-monotonic seq at line {line_number}: {self.path}"
                )
            self._validate_turn_progression(
                line_number=line_number,
                message=message,
                prior_messages=messages,
                state=turn_progression,
            )
            messages.append(message)
        return messages

    def _iter_dicts(self) -> Iterator[tuple[int, dict]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Transcript contains invalid JSON at line {line_number}: {self.path}"
                    ) from exc
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Transcript contains a non-object message at line {line_number}: {self.path}"
                    )
                yield line_number, item

    def _validate_message(self, *, line_number: int, message: Message) -> None:
        if not is_strict_int(message.seq):
            raise ValueError(
                f"Transcript contains an invalid message shape at line {line_number}: {self.path}"
            )
        if not isinstance(message.timestamp, str):
            raise ValueError(
                f"Transcript contains an invalid message shape at line {line_number}: {self.path}"
            )
        if not isinstance(message.role, str):
            raise ValueError(
                f"Transcript contains an invalid message shape at line {line_number}: {self.path}"
            )
        if not isinstance(message.author, str):
            raise ValueError(
                f"Transcript contains an invalid message shape at line {line_number}: {self.path}"
            )
        if not isinstance(message.content, str):
            raise ValueError(
                f"Transcript contains an invalid message shape at line {line_number}: {self.path}"
            )
        if not isinstance(message.metadata, dict):
            raise ValueError(
                f"Transcript contains an invalid message shape at line {line_number}: {self.path}"
            )

    def _validate_topic_invariants(
        self, *, line_number: int, message: Message, prior_messages: list[Message]
    ) -> None:
        topic_kind = message.metadata.get("kind") == "topic"
        if not prior_messages:
            if not topic_kind:
                raise ValueError(
                    f"Transcript must begin with the original topic message at line {line_number}: {self.path}"
                )
            if message.role != "moderator":
                raise ValueError(
                    f"Transcript topic message must have moderator role at line {line_number}: {self.path}"
                )
            return

        if topic_kind:
            raise ValueError(f"Transcript contains multiple topic messages at line {line_number}: {self.path}")

    def _validate_protocol_message(self, *, line_number: int, message: Message) -> None:
        if message.role not in {"moderator", "agent", "system"}:
            raise ValueError(f"Transcript contains an invalid role at line {line_number}: {self.path}")

        kind = message.metadata.get("kind")
        if kind is not None and not isinstance(kind, str):
            raise ValueError(f"Transcript contains an invalid metadata kind at line {line_number}: {self.path}")

        if message.role == "moderator":
            if kind not in {"topic", "interjection"}:
                raise ValueError(
                    f"Transcript moderator messages must declare topic or interjection kind at line {line_number}: "
                    f"{self.path}"
                )
            if kind == "interjection":
                self._validate_positive_int(
                    line_number=line_number,
                    value=message.metadata.get("turn"),
                    error_prefix="Transcript moderator interjections must declare a positive turn",
                )
            return

        if message.role == "agent":
            if kind is not None:
                raise ValueError(
                    f"Transcript agent messages cannot declare a metadata kind at line {line_number}: {self.path}"
                )
            if not isinstance(message.metadata.get("provider"), str) or not isinstance(message.metadata.get("model"), str):
                raise ValueError(
                    f"Transcript agent messages must declare provider and model strings at line {line_number}: "
                    f"{self.path}"
                )
            self._validate_positive_int(
                line_number=line_number,
                value=message.metadata.get("turn"),
                error_prefix="Transcript agent messages must declare a positive turn",
            )
            return

        if message.author != "relay":
            raise ValueError(
                f"Transcript system messages must be authored by relay at line {line_number}: {self.path}"
            )
        if kind not in {"attempt_failed", "provider_request", "policy_gate", "pause"}:
            raise ValueError(
                "Transcript system messages must declare attempt_failed, provider_request, policy_gate, or pause kind "
                f"at line {line_number}: {self.path}"
            )
        if kind == "attempt_failed":
            if not isinstance(message.metadata.get("speaker"), str) or not isinstance(
                message.metadata.get("failure_type"), str
            ):
                raise ValueError(
                    "Transcript attempt_failed messages must declare speaker and failure_type strings "
                    f"at line {line_number}: {self.path}"
                )
            self._validate_positive_int(
                line_number=line_number,
                value=message.metadata.get("turn"),
                error_prefix="Transcript attempt_failed messages must declare a positive turn",
            )
            return
        if kind == "provider_request":
            if not isinstance(message.metadata.get("speaker"), str) or not isinstance(
                message.metadata.get("provider"), str
            ):
                raise ValueError(
                    "Transcript provider_request messages must declare speaker and provider strings "
                    f"at line {line_number}: {self.path}"
                )
            self._validate_positive_int(
                line_number=line_number,
                value=message.metadata.get("turn"),
                error_prefix="Transcript provider_request messages must declare a positive turn",
            )
            return
        if kind == "policy_gate":
            if not isinstance(message.metadata.get("speaker"), str) or not isinstance(
                message.metadata.get("decision"), str
            ):
                raise ValueError(
                    "Transcript policy_gate messages must declare speaker and decision strings "
                    f"at line {line_number}: {self.path}"
                )
            blockers = message.metadata.get("blockers")
            if blockers is not None and (
                not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers)
            ):
                raise ValueError(
                    f"Transcript policy_gate messages must declare string blocker details at line {line_number}: "
                    f"{self.path}"
                )
            self._validate_positive_int(
                line_number=line_number,
                value=message.metadata.get("turn"),
                error_prefix="Transcript policy_gate messages must declare a positive turn",
            )
            return

        if message.metadata.get("status") != "paused" or not isinstance(message.metadata.get("reason"), str):
            raise ValueError(
                f"Transcript pause messages must declare paused status and string reason at line {line_number}: "
                f"{self.path}"
            )
        self._validate_positive_int(
            line_number=line_number,
            value=message.metadata.get("next_turn"),
            error_prefix="Transcript pause messages must declare a positive next_turn",
        )
        fault_state = message.metadata.get("fault_state")
        if fault_state is not None and not is_valid_fault_state_snapshot(fault_state):
            raise ValueError(
                f"Transcript pause messages must declare valid fault_state metadata at line {line_number}: "
                f"{self.path}"
            )
        policy_state = message.metadata.get("policy_state")
        if policy_state is not None and not is_valid_policy_state_snapshot(policy_state):
            raise ValueError(
                f"Transcript pause messages must declare valid policy_state metadata at line {line_number}: "
                f"{self.path}"
            )

    def _validate_positive_int(self, *, line_number: int, value: object, error_prefix: str) -> None:
        if not is_strict_int(value) or value < 1:
            raise ValueError(f"{error_prefix} at line {line_number}: {self.path}")

    def _validate_session_consistency(
        self, *, line_number: int, message: Message, prior_messages: list[Message]
    ) -> None:
        session = self._load_expected_session(message=message, prior_messages=prior_messages)
        if session is None:
            return
        expected_events = session["moderator_events"]
        observed_interjections = [
            prior
            for prior in prior_messages
            if prior.role == "moderator" and prior.metadata.get("kind") == "interjection"
        ]

        if not prior_messages:
            if message.author != session["moderator"]:
                raise ValueError(
                    f"Transcript topic author does not match the stored moderator at line {line_number}: {self.path}"
                )
            return

        if message.role == "moderator":
            if message.metadata.get("kind") != "interjection":
                return
            index = len(observed_interjections)
            if index < len(expected_events):
                # Validate pre-scripted interjections against stored session
                expected_event = expected_events[index]
                if (
                    message.metadata.get("turn") != expected_event["turn"]
                    or message.author != expected_event["author"]
                    or message.content != expected_event["content"]
                ):
                    raise ValueError(
                        f"Transcript moderator interjection does not match the stored session at line {line_number}: "
                        f"{self.path}"
                    )
            # Extra interjections beyond stored events are allowed (live moderator input)
            return

        completed_turn = self._completed_turn(message)
        if completed_turn is not None:
            expected_event_count = sum(1 for event in expected_events if event["turn"] <= completed_turn)
            if len(observed_interjections) < expected_event_count:
                raise ValueError(
                    f"Transcript moderator interjections do not match the stored session at line {line_number}: "
                    f"{self.path}"
                )

        if message.role == "agent":
            expected_agent = self._expected_agent_for_turn(session=session, turn=message.metadata["turn"])
            if message.author != expected_agent["name"]:
                raise ValueError(
                    f"Transcript agent author does not match the stored session at line {line_number}: {self.path}"
                )
            if (
                message.metadata.get("provider") != expected_agent["provider"]
                or message.metadata.get("model") != expected_agent["model"]
            ):
                raise ValueError(
                    f"Transcript agent metadata does not match the stored session at line {line_number}: {self.path}"
                )
            return

        if message.role != "system":
            return

        kind = message.metadata.get("kind")
        if kind == "pause":
            expected_digest = self._load_expected_resume_state_digest(message=message, prior_messages=prior_messages)
            if message.metadata.get("resume_state_digest") != expected_digest:
                raise ValueError(
                    "Transcript pause resume_state_digest does not match the stored topic and session "
                    f"at line {line_number}: {self.path}"
                )
            expected_conversation_digest = self._load_expected_conversation_digest(prior_messages=prior_messages)
            if message.metadata.get("conversation_digest") != expected_conversation_digest:
                raise ValueError(
                    "Transcript pause conversation_digest does not match the stored conversation prefix "
                    f"at line {line_number}: {self.path}"
                )
            return

        if kind not in {"attempt_failed", "provider_request", "policy_gate"}:
            return

        expected_agent = self._expected_agent_for_turn(session=session, turn=message.metadata["turn"])
        if message.metadata.get("speaker") != expected_agent["name"]:
            raise ValueError(
                f"Transcript system speaker does not match the stored session at line {line_number}: {self.path}"
            )
        if kind == "provider_request" and message.metadata.get("provider") != expected_agent["provider"]:
            raise ValueError(
                f"Transcript provider_request metadata does not match the stored session at line {line_number}: "
                f"{self.path}"
            )

    def _validate_turn_progression(
        self,
        *,
        line_number: int,
        message: Message,
        prior_messages: list[Message],
        state: dict[str, object],
    ) -> None:
        if self._load_expected_session(message=message, prior_messages=prior_messages) is None:
            return

        kind = message.metadata.get("kind")
        if kind == "topic":
            return

        if state["stage"] == "paused":
            # A pause ends one relay invocation; the next row, if any, starts the resumed segment.
            state["stage"] = "interjections"

        if state["stage"] == "after_terminal" and kind != "pause":
            state["stage"] = "interjections"

        current_turn = state["current_turn"]
        stage = state["stage"]

        if message.role == "moderator":
            if stage != "interjections" or message.metadata.get("turn") != current_turn:
                raise ValueError(
                    f"Transcript turn order does not match relay execution at line {line_number}: {self.path}"
                )
            return

        if message.role == "agent":
            if stage not in {"interjections", "after_provider_request"} or message.metadata.get("turn") != current_turn:
                raise ValueError(
                    f"Transcript turn order does not match relay execution at line {line_number}: {self.path}"
                )
            state["current_turn"] = current_turn + 1
            state["stage"] = "after_terminal"
            return

        if kind == "provider_request":
            if stage != "interjections" or message.metadata.get("turn") != current_turn:
                raise ValueError(
                    f"Transcript turn order does not match relay execution at line {line_number}: {self.path}"
                )
            state["stage"] = "after_provider_request"
            return

        if kind == "attempt_failed":
            if stage not in {"interjections", "after_provider_request"} or message.metadata.get("turn") != current_turn:
                raise ValueError(
                    f"Transcript turn order does not match relay execution at line {line_number}: {self.path}"
                )
            state["current_turn"] = current_turn + 1
            state["stage"] = "after_terminal"
            return

        if kind == "policy_gate":
            if stage not in {"interjections", "after_provider_request"} or message.metadata.get("turn") != current_turn:
                raise ValueError(
                    f"Transcript turn order does not match relay execution at line {line_number}: {self.path}"
                )
            state["current_turn"] = current_turn + 1
            state["stage"] = "after_terminal"
            return

        if kind == "pause" and (stage != "after_terminal" or message.metadata.get("next_turn") != current_turn):
            raise ValueError(
                f"Transcript turn order does not match relay execution at line {line_number}: {self.path}"
            )
        if kind == "pause":
            state["stage"] = "paused"

    def _load_expected_session(
        self, *, message: Message, prior_messages: list[Message]
    ) -> dict[str, object] | None:
        topic_message = message if not prior_messages else prior_messages[0]
        session = topic_message.metadata.get("session")
        if not is_valid_session_snapshot(session):
            return None

        return {
            "moderator": session["moderator"],
            "moderator_events": self._load_expected_moderator_events(session["moderator_events"]),
            "left_agent": self._load_expected_agent(session["left_agent"]),
            "right_agent": self._load_expected_agent(session["right_agent"]),
        }

    def _load_expected_resume_state_digest(
        self, *, message: Message, prior_messages: list[Message]
    ) -> str | None:
        topic_message = message if not prior_messages else prior_messages[0]
        session = topic_message.metadata.get("session")
        if not is_valid_session_snapshot(session):
            return None
        fault_state = message.metadata.get("fault_state")
        if fault_state is not None and not is_valid_fault_state_snapshot(fault_state):
            return None
        policy_state = message.metadata.get("policy_state")
        if policy_state is not None and not is_valid_policy_state_snapshot(policy_state):
            return None
        return compute_resume_state_digest(
            topic=topic_message.content,
            session=session,
            fault_state=fault_state,
            policy_state=policy_state,
        )

    @staticmethod
    def _load_expected_conversation_digest(*, prior_messages: list[Message]) -> str:
        return compute_conversation_digest(prior_messages)

    def _load_expected_moderator_events(self, payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, list):
            return []

        events: list[dict[str, object]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            events.append(
                {
                    "turn": item.get("turn"),
                    "author": item.get("author"),
                    "content": item.get("content"),
                }
            )
        return sorted(events, key=lambda item: item["turn"])

    def _load_expected_agent(self, payload: object) -> dict[str, str] | None:
        if not isinstance(payload, dict):
            return None

        name = payload.get("name")
        provider = payload.get("provider")
        model = payload.get("model")
        if not isinstance(name, str) or not isinstance(provider, str) or not isinstance(model, str):
            return None
        return {"name": name, "provider": provider, "model": model}

    def _completed_turn(self, message: Message) -> int | None:
        if message.role == "agent":
            return message.metadata.get("turn")
        if message.role != "system":
            return None
        kind = message.metadata.get("kind")
        if kind in {"attempt_failed", "provider_request"}:
            return message.metadata.get("turn")
        if kind == "pause":
            return message.metadata.get("next_turn", 1) - 1
        return None

    def _expected_agent_for_turn(self, *, session: dict[str, object], turn: int) -> dict[str, str]:
        side = "left_agent" if turn % 2 == 1 else "right_agent"
        return session[side]  # type: ignore[return-value]
