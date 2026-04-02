from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from .models import (
    AgentConfig,
    Message,
    ModeratorEvent,
    RelayConfig,
    RelayRunResult,
    compute_conversation_digest,
    compute_resume_state_digest,
    is_strict_int,
    is_valid_fault_state_snapshot,
    is_valid_policy_state_snapshot,
    is_valid_session_snapshot,
    utc_now,
)
from .policy import Decision as PolicyDecision
from .policy_relay import RelayPolicyHarness, classify_relay_action
from .providers import BaseProvider, ProviderError, get_provider
from .transcript import TranscriptStore


class RelayRunner:
    def __init__(
        self,
        config: RelayConfig,
        out_path: Path,
        moderator_queue: object | None = None,
        on_commit: object | None = None,
        on_stream_chunk: object | None = None,
        on_activity: object | None = None,
        workspace_path: Path | None = None,
    ) -> None:
        self.config = config
        self.out_path = out_path
        self._store = TranscriptStore(out_path)
        self._moderator_queue = moderator_queue  # ModeratorInputQueue (optional)
        self._on_commit = on_commit  # Callable[[Message], None] (optional)
        self._on_activity = on_activity  # Callable[[dict], None] (optional)
        self._on_stream_chunk = on_stream_chunk  # Callable[[str], None] (optional)
        self._observer = None  # SessionObserver (optional)
        self._workspace_path = workspace_path
        self._workspace_mgr = None
        if self._workspace_path is not None:
            from .workspace import WorkspaceManager

            self._workspace_mgr = WorkspaceManager(self._workspace_path)
            self._workspace_mgr.setup(
                left_name=self.config.left_agent.name,
                right_name=self.config.right_agent.name,
            )
        self._providers: dict[str, BaseProvider] = {}
        self._fault_scripts = {
            id(config.left_agent): list(config.left_agent.fault_script),
            id(config.right_agent): list(config.right_agent.fault_script),
        }
        self._policy = RelayPolicyHarness(use_harness=config.use_harness)

    def _get_provider(self, agent: AgentConfig) -> BaseProvider:
        """Return a cached provider for the given agent."""
        side = "left" if agent is self.config.left_agent else "right"
        if side not in self._providers:
            kwargs: dict[str, object] = {}
            if agent.provider in ("cli-claude", "cli-codex") and self._workspace_path:
                kwargs["workspace_path"] = self._workspace_path
            self._providers[side] = get_provider(agent.provider, **kwargs)
        return self._providers[side]

    def set_observer(self, observer: object) -> None:
        """Attach a SessionObserver for timing and metrics."""
        self._observer = observer

    def run(self, *, resume: bool = False) -> RelayRunResult:
        if self._observer:
            self._observer.on_session_start()
        result = self._run_inner(resume=resume)
        if self._observer:
            self._observer.on_session_end(result.status)
        return result

    def _run_inner(self, *, resume: bool = False) -> RelayRunResult:
        events = self._group_events(self.config.moderator_events)
        append_counts = self._empty_agent_counter()
        failed_attempts = self._empty_agent_counter()

        if resume:
            messages = self._store.load_messages()
            if not messages:
                raise ValueError("Cannot resume an empty transcript")
            self._validate_resume_session(messages)
            self._restore_fault_state(messages)
            self._restore_policy_state(messages)
            sequence = messages[-1].seq + 1
            start_turn = self._load_resume_turn(messages)
            if start_turn > self.config.turns:
                raise ValueError(
                    "Cannot resume: stored next_turn "
                    f"{start_turn} exceeds configured turns {self.config.turns}"
                )
        else:
            messages = []
            sequence = 1
            start_turn = 1
            topic_message = self._build_message(
                seq=sequence,
                role="moderator",
                author=self.config.moderator,
                content=self.config.topic,
                metadata={"kind": "topic", "session": self._session_snapshot()},
            )
            self._commit(messages, topic_message)
            sequence += 1

        agents = (self.config.left_agent, self.config.right_agent)
        turn = start_turn
        while turn <= self.config.turns:
            for event in events.get(turn, []):
                moderator_message = self._build_message(
                    seq=sequence,
                    role="moderator",
                    author=event.author,
                    content=event.content,
                    metadata={"kind": "interjection", "turn": turn},
                )
                self._commit(messages, moderator_message)
                sequence += 1

            # Drain live moderator input queue (if connected)
            if self._moderator_queue is not None:
                ctrl_result = self._drain_moderator_queue(
                    messages=messages, sequence=sequence, turn=turn,
                )
                # Update sequence from any committed messages
                sequence = messages[-1].seq + 1 if messages else sequence
                if ctrl_result is not None:
                    status, reason, _ = ctrl_result
                    if status == "stopped":
                        return RelayRunResult(messages=messages, status="completed")
                    if status == "paused":
                        pause_message = self._build_pause_message(
                            sequence=sequence, reason=reason, next_turn=turn, messages=messages,
                        )
                        self._commit(messages, pause_message)
                        return RelayRunResult(messages=messages, status="paused", pause_reason=reason)

            agent = agents[(turn - 1) % len(agents)]
            request_trace = self._provider_request_trace(agent=agent, transcript=messages, turn=turn)
            if request_trace:
                trace_message = self._build_message(
                    seq=sequence,
                    role="system",
                    author="relay",
                    content=f"Prepared {agent.provider} request for {agent.name} on turn {turn}.",
                    metadata=request_trace,
                )
                self._commit(messages, trace_message)
                sequence += 1

            if self._observer:
                self._observer.on_turn_start(turn, agent.name)

            self._emit_activity({
                "kind": "thinking",
                "agent": agent.name,
                "turn": turn,
                "provider": agent.provider,
            })

            response, failure_type, failure_reason = self._attempt_with_retry(
                agent=agent, transcript=messages, turn=turn,
            )
            if failure_reason is not None:
                if self._observer:
                    self._observer.on_turn_end(turn, agent.name, success=False, failure_type=failure_type)
                failed_attempts[agent.name] += 1
                failure_message = self._build_message(
                    seq=sequence,
                    role="system",
                    author="relay",
                    content=f"{agent.name} attempt failed on turn {turn}: {failure_reason}",
                    metadata={
                        "kind": "attempt_failed",
                        "speaker": agent.name,
                        "turn": turn,
                        "failure_type": failure_type,
                    },
                )
                self._commit(messages, failure_message)
                sequence += 1

                pause_reason = self._check_failed_attempts(agent.name, failed_attempts[agent.name])
                if pause_reason:
                    pause_message = self._build_pause_message(
                        sequence=sequence,
                        reason=pause_reason,
                        next_turn=turn + 1,
                        messages=messages,
                    )
                    self._commit(messages, pause_message)
                    return RelayRunResult(messages=messages, status="paused", pause_reason=pause_reason)
                turn += 1
                continue

            agent_message = self._build_message(
                seq=sequence,
                role="agent",
                author=agent.name,
                content=response,
                metadata={"provider": agent.provider, "model": agent.model, "turn": turn},
            )
            policy_result = self._policy.evaluate_turn(agent.name, response, messages)
            action_type = classify_relay_action(agent_message, messages)

            self._emit_activity({
                "kind": "harness_eval",
                "agent": agent.name,
                "turn": turn,
                "action_type": action_type,
                "decision": policy_result.decision.value,
                "allowed": policy_result.allowed,
                "blockers": [b.detail for b in policy_result.blockers] if policy_result.blockers else [],
            })

            if not policy_result.allowed:
                self._policy.record_outcome(agent.name, response, "denied", action_type)
                gate_message = self._build_policy_gate_message(
                    sequence=sequence,
                    agent=agent,
                    turn=turn,
                    response=response,
                    decision=policy_result.decision.value,
                    blockers=[blocker.detail for blocker in policy_result.blockers],
                )
                self._commit(messages, gate_message)
                sequence += 1
                if self._observer:
                    self._observer.on_turn_end(
                        turn,
                        agent.name,
                        success=False,
                        failure_type=f"policy_{policy_result.decision.value}",
                    )
                if policy_result.decision == PolicyDecision.CLARIFY:
                    pause_reason = (
                        f"Paused relay: policy requires clarification before committing "
                        f"{agent.name}'s turn."
                    )
                    pause_message = self._build_pause_message(
                        sequence=sequence,
                        reason=pause_reason,
                        next_turn=turn + 1,
                        messages=messages,
                    )
                    self._commit(messages, pause_message)
                    return RelayRunResult(messages=messages, status="paused", pause_reason=pause_reason)
                turn += 1
                continue

            if action_type == "request_permission":
                self._policy.record_outcome(agent.name, response, "denied", action_type)
                gate_message = self._build_policy_gate_message(
                    sequence=sequence,
                    agent=agent,
                    turn=turn,
                    response=response,
                    decision="block",
                    blockers=[
                        "Permission requests are not executable relay actions in this environment. "
                        "Use the workspace and available tools directly."
                    ],
                )
                self._commit(messages, gate_message)
                sequence += 1
                if self._observer:
                    self._observer.on_turn_end(
                        turn,
                        agent.name,
                        success=False,
                        failure_type="policy_block",
                    )
                turn += 1
                continue

            self._commit(messages, agent_message)
            sequence += 1
            self._policy.record_outcome(agent.name, response, "success", action_type)

            self._emit_activity({
                "kind": "turn_committed",
                "agent": agent.name,
                "turn": turn,
                "action_type": action_type,
            })

            if self._observer:
                self._observer.on_turn_end(turn, agent.name, success=True)

            append_counts[agent.name] += 1
            failed_attempts[agent.name] = 0
            self._forward_workspace_outbox(agent=agent)

            pause_reason = self._check_operator_tripwire(agent_message.content) or self._check_one_sided_appends(
                append_counts
            )
            if pause_reason:
                pause_message = self._build_pause_message(
                    sequence=sequence,
                    reason=pause_reason,
                    next_turn=turn + 1,
                    messages=messages,
                )
                self._commit(messages, pause_message)
                return RelayRunResult(messages=messages, status="paused", pause_reason=pause_reason)

            turn += 1

        return RelayRunResult(messages=messages, status="completed")

    def _empty_agent_counter(self) -> dict[str, int]:
        return {
            self.config.left_agent.name: 0,
            self.config.right_agent.name: 0,
        }

    def _forward_workspace_outbox(self, agent: AgentConfig) -> None:
        if self._workspace_mgr is None:
            return

        peer = self.config.right_agent if agent is self.config.left_agent else self.config.left_agent
        self._workspace_mgr.forward_outbox(agent.name, peer.name)

    def _emit_activity(self, activity: dict) -> None:
        """Emit an activity event (thinking, harness eval, etc.)."""
        if self._on_activity is not None:
            self._on_activity(activity)

    def _commit(self, messages: list[Message], message: Message) -> None:
        self._store.append(message)
        messages.append(message)
        if self._on_commit is not None:
            self._on_commit(message)

    def _load_resume_turn(self, messages: list[Message]) -> int:
        last = messages[-1]
        if last.role != "system" or last.metadata.get("kind") != "pause":
            raise ValueError("Transcript is not paused")
        next_turn = last.metadata.get("next_turn")
        if not is_strict_int(next_turn):
            raise ValueError("Paused transcript is missing next_turn metadata")
        return next_turn

    def _validate_resume_session(self, messages: list[Message]) -> None:
        topic_message = next((message for message in messages if message.metadata.get("kind") == "topic"), None)
        if topic_message is None:
            raise ValueError("Transcript is missing the original topic message")

        stored_session = topic_message.metadata.get("session")
        if not isinstance(stored_session, dict):
            raise ValueError("Cannot safely resume transcript without stored session metadata")
        if not is_valid_session_snapshot(stored_session):
            raise ValueError("Cannot safely resume transcript with invalid stored session metadata")

        if topic_message.content != self.config.topic:
            raise ValueError("Cannot resume: configured topic does not match the stored transcript topic")

        if stored_session != self._session_snapshot():
            raise ValueError("Cannot resume: configured session does not match the stored transcript session")

    def _attempt_with_retry(
        self,
        agent: AgentConfig,
        transcript: list[Message],
        turn: int,
    ) -> tuple[str, str | None, str | None]:
        """Attempt agent call with retry-and-backoff on failure."""
        import time as _time

        max_retries = self.config.retry_attempts
        backoff = self.config.retry_backoff_seconds

        for attempt in range(1 + max_retries):
            response, failure_type, failure_reason = self._attempt_agent(
                agent=agent, transcript=transcript, turn=turn,
            )
            if failure_reason is None:
                return response, None, None

            # Don't retry on fault injection (those are intentional)
            if failure_type in ("timeout", "error", "empty") and failure_reason.startswith("Injected"):
                return response, failure_type, failure_reason

            if attempt < max_retries:
                wait = backoff * (2 ** attempt)
                if self._on_commit:
                    # Notify via commit callback that we're retrying
                    pass
                _time.sleep(wait)

        return response, failure_type, failure_reason

    def _attempt_agent(
        self,
        agent: AgentConfig,
        transcript: list[Message],
        turn: int,
    ) -> tuple[str, str | None, str | None]:
        fault = self._consume_fault(agent)
        if fault == "timeout":
            return "", "timeout", "Injected timeout"
        if fault == "error":
            return "", "error", "Injected provider error"
        if fault == "empty":
            return "", "empty", "Injected empty response"
        if fault == "operator":
            return "If you guys can't implement it, let me know where the limit is.", None, None
        if fault not in (None, "ok"):
            raise ValueError(f"Unknown fault mode '{fault}' for agent {agent.name}")

        try:
            provider = self._get_provider(agent)
            if self._on_stream_chunk and provider.supports_streaming:
                chunks: list[str] = []
                for chunk in provider.generate_stream(agent=agent, transcript=transcript, turn=turn):
                    chunks.append(chunk)
                    self._on_stream_chunk(chunk)
                response = "".join(chunks)
            else:
                response = provider.generate(agent=agent, transcript=transcript, turn=turn)
        except ProviderError as exc:
            return "", "provider_error", str(exc)
        except Exception as exc:
            details = f"Unexpected provider exception: {type(exc).__name__}: {exc}"
            return "", "unexpected_error", details

        if not response or not response.strip():
            return "", "empty", "Provider returned no content"
        return response.strip(), None, None

    def _consume_fault(self, agent: AgentConfig) -> str | None:
        fault_script = self._fault_scripts.setdefault(id(agent), list(agent.fault_script))
        if not fault_script:
            return None
        return self._normalize_fault(fault_script.pop(0))

    @staticmethod
    def _normalize_fault(fault: str) -> str | None:
        normalized = fault.strip().lower()
        return normalized or None

    def _peek_fault(self, agent: AgentConfig) -> str | None:
        fault_script = self._fault_scripts.setdefault(id(agent), list(agent.fault_script))
        if not fault_script:
            return None
        return self._normalize_fault(fault_script[0])

    def _provider_request_trace(self, agent: AgentConfig, transcript: list[Message], turn: int) -> dict | None:
        if not self.config.trace_provider_payloads:
            return None
        if self._peek_fault(agent) not in (None, "ok"):
            return None

        try:
            preview = self._get_provider(agent).preview_request(agent=agent, transcript=transcript, turn=turn)
        except Exception:
            return None
        if not preview:
            return None

        return {
            "kind": "provider_request",
            "speaker": agent.name,
            "turn": turn,
            "provider": agent.provider,
            **preview,
        }

    def _drain_moderator_queue(
        self,
        messages: list[Message],
        sequence: int,
        turn: int,
    ) -> tuple[str, str, int] | None:
        """Drain pending entries from the live moderator queue.

        Returns (status, reason, seq_delta) if a control action should interrupt
        the loop, or None to continue normally.
        """
        from .moderator import ControlCommand, ModeratorMessage

        seq_delta = 0
        for entry in self._moderator_queue.drain():
            if isinstance(entry, ControlCommand):
                if entry.command == "stop":
                    return ("stopped", "Stopped by moderator.", seq_delta)
                if entry.command == "pause":
                    return ("paused", "Paused by moderator.", seq_delta)
                if entry.command == "more" and entry.value:
                    self.config.turns += entry.value
                    return ("extended", f"Extended by {entry.value} turns.", seq_delta)
                if entry.command == "nolimit":
                    self.config.turns = 999_999
                    return ("extended", "Turn limit removed.", seq_delta)
            elif isinstance(entry, ModeratorMessage):
                msg = self._build_message(
                    seq=sequence + seq_delta,
                    role="moderator",
                    author=self.config.moderator,
                    content=entry.content,
                    metadata={"kind": "interjection", "turn": turn},
                )
                self._commit(messages, msg)
                seq_delta += 1
        return None if seq_delta == 0 else None

    def _check_failed_attempts(self, speaker: str, failures: int) -> str | None:
        if failures < self.config.max_failed_attempts:
            return None
        return (
            f"Paused relay: {speaker} reached {failures} consecutive failed attempts "
            "without a transcript append."
        )

    def _check_one_sided_appends(self, append_counts: dict[str, int]) -> str | None:
        total_appends = sum(append_counts.values())
        if total_appends < self.config.max_total_appends_without_both:
            return None

        missing = [speaker for speaker, count in append_counts.items() if count == 0]
        if not missing:
            return None

        speakers = ", ".join(missing)
        return f"Paused relay: {speakers} still has 0 committed messages after {total_appends} agent appends."

    def _check_operator_tripwire(self, content: str) -> str | None:
        for pattern in self.config.operator_tripwire_patterns:
            if re.search(pattern, content, flags=re.IGNORECASE):
                return "Paused relay: committed agent message matched the operator-language tripwire."
        return None

    def _build_pause_message(self, sequence: int, reason: str, next_turn: int, messages: list[Message]) -> Message:
        return self._build_message(
            seq=sequence,
            role="system",
            author="relay",
            content=reason,
            metadata={
                "kind": "pause",
                "status": "paused",
                "reason": reason,
                "next_turn": next_turn,
                "fault_state": self._fault_state_snapshot(),
                "policy_state": self._policy.export_state(),
                "resume_state_digest": self._resume_state_digest(),
                "conversation_digest": self._conversation_digest(messages),
            },
        )

    def _session_snapshot(self) -> dict[str, object]:
        return {
            "moderator": self.config.moderator,
            "moderator_events": [
                {"turn": event.turn, "content": event.content, "author": event.author}
                for event in self.config.moderator_events
            ],
            "left_agent": self._agent_snapshot(self.config.left_agent),
            "right_agent": self._agent_snapshot(self.config.right_agent),
        }

    @staticmethod
    def _agent_snapshot(agent: AgentConfig) -> dict[str, str]:
        return {
            "name": agent.name,
            "provider": agent.provider,
            "model": agent.model,
            "instruction": agent.instruction,
        }

    def _resume_state_digest(self) -> str:
        return compute_resume_state_digest(
            topic=self.config.topic,
            session=self._session_snapshot(),
            fault_state=self._fault_state_snapshot(),
            policy_state=self._policy.export_state(),
        )

    def _fault_state_snapshot(self) -> dict[str, list[str]]:
        return {
            "left_agent": list(self._fault_scripts.get(id(self.config.left_agent), [])),
            "right_agent": list(self._fault_scripts.get(id(self.config.right_agent), [])),
        }

    def _restore_fault_state(self, messages: list[Message]) -> None:
        last = messages[-1]
        if last.role != "system" or last.metadata.get("kind") != "pause":
            raise ValueError("Transcript is not paused")

        stored_fault_state = last.metadata.get("fault_state")
        if stored_fault_state is None:
            return
        if not is_valid_fault_state_snapshot(stored_fault_state):
            raise ValueError("Paused transcript is missing valid fault_state metadata")

        self._fault_scripts[id(self.config.left_agent)] = list(stored_fault_state["left_agent"])
        self._fault_scripts[id(self.config.right_agent)] = list(stored_fault_state["right_agent"])

    def _restore_policy_state(self, messages: list[Message]) -> None:
        last = messages[-1]
        if last.role != "system" or last.metadata.get("kind") != "pause":
            raise ValueError("Transcript is not paused")

        stored_policy_state = last.metadata.get("policy_state")
        if stored_policy_state is None:
            return
        if not is_valid_policy_state_snapshot(stored_policy_state):
            raise ValueError("Paused transcript is missing valid policy_state metadata")
        self._policy.restore_state(stored_policy_state)

    @staticmethod
    def _conversation_digest(messages: list[Message]) -> str:
        return compute_conversation_digest(messages)

    @staticmethod
    def _group_events(events: list[ModeratorEvent]) -> dict[int, list[ModeratorEvent]]:
        grouped: dict[int, list[ModeratorEvent]] = defaultdict(list)
        for event in sorted(events, key=lambda item: item.turn):
            grouped[event.turn].append(event)
        return grouped

    @staticmethod
    def _build_message(
        seq: int,
        role: str,
        author: str,
        content: str,
        metadata: dict,
    ) -> Message:
        return Message(
            seq=seq,
            timestamp=utc_now(),
            role=role,
            author=author,
            content=content,
            metadata=metadata,
        )

    def _build_policy_gate_message(
        self,
        *,
        sequence: int,
        agent: AgentConfig,
        turn: int,
        response: str,
        decision: str,
        blockers: list[str],
    ) -> Message:
        reason = blockers[0] if blockers else "Policy blocked the proposed turn."
        return self._build_message(
            seq=sequence,
            role="system",
            author="relay",
            content=f"Blocked {agent.name} turn {turn}: {reason}",
            metadata={
                "kind": "policy_gate",
                "speaker": agent.name,
                "turn": turn,
                "decision": decision,
                "action_type": classify_relay_action(
                    Message(
                        seq=0,
                        timestamp="",
                        role="agent",
                        author=agent.name,
                        content=response,
                    ),
                    [],
                ),
                "blockers": blockers,
            },
        )
