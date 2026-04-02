from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from relay_discussion import engine as engine_module
from relay_discussion import providers as providers_module
from relay_discussion import cli as cli_module
from relay_discussion.engine import RelayRunner
from relay_discussion.models import AgentConfig, Message, ModeratorEvent, RelayConfig
from relay_discussion.providers import AnthropicProvider, BaseProvider, MockProvider
from relay_discussion.transcript import TranscriptStore


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "relay_discussion.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd or _repo_root(),
    )


class FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_relay_persists_ordered_transcript(tmp_path: Path) -> None:
    out_path = tmp_path / "transcript.jsonl"
    config = RelayConfig(
        topic="Debate whether the relay should enforce turn limits.",
        turns=4,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        moderator_events=[
            ModeratorEvent(turn=2, content="Push on failure modes."),
            ModeratorEvent(turn=4, content="End with implementation implications."),
        ],
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    stored = TranscriptStore(out_path).read()

    assert result.status == "completed"
    assert [message.seq for message in result.messages] == list(range(1, len(result.messages) + 1))
    assert [item["seq"] for item in stored] == list(range(1, len(stored) + 1))
    assert stored[0]["author"] == "Satisho"
    assert stored[1]["author"] == "Claude"
    assert stored[2]["author"] == "Satisho"
    assert stored[3]["author"] == "Codex"
    assert stored[-2]["author"] == "Satisho"
    assert stored[-1]["author"] == "Codex"


def test_mock_responses_track_latest_context(tmp_path: Path) -> None:
    out_path = tmp_path / "context.jsonl"
    config = RelayConfig(
        topic="Find the weakest assumption in the proposed architecture.",
        turns=2,
        left_agent=AgentConfig(name="Claude", instruction="Surface tradeoffs."),
        right_agent=AgentConfig(name="Codex", instruction="Move from critique to action."),
    )

    result = RelayRunner(config=config, out_path=out_path).run()

    assert result.status == "completed"
    assert "responding to Satisho" in result.messages[1].content
    assert "Surface tradeoffs." in result.messages[1].content
    assert "responding to Claude" in result.messages[2].content
    assert "Move from critique to action." in result.messages[2].content


def test_build_mode_forwards_outbox_to_peer_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "build_mode.jsonl"
    workspace_path = tmp_path / "workspace"
    seen_inboxes: list[str] = []

    class WorkspaceAwareProvider(BaseProvider):
        def generate(self, agent: AgentConfig, transcript: list[Message], turn: int) -> str:
            if agent.name == "Claude":
                (workspace_path / "claude" / "outbox.md").write_text("Review shared/main.py")
                return "Claude asked Codex to inspect the workspace."

            seen_inboxes.append((workspace_path / "codex" / "inbox.md").read_text())
            return "Codex saw the forwarded inbox message."

    monkeypatch.setattr(engine_module, "get_provider", lambda name, **kwargs: WorkspaceAwareProvider())

    config = RelayConfig(
        topic="Build mode should actually relay workspace mailbox traffic.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )

    result = RelayRunner(config=config, out_path=out_path, workspace_path=workspace_path).run()

    assert result.status == "completed"
    assert seen_inboxes == ["[Claude]: Review shared/main.py\n"]
    assert (workspace_path / "claude" / "outbox.md").read_text() == ""
    assert (workspace_path / "codex" / "inbox.md").read_text() == "[Claude]: Review shared/main.py\n"


def test_cli_creates_jsonl_transcript(tmp_path: Path) -> None:
    out_path = tmp_path / "cli_transcript.jsonl"
    moderator_path = tmp_path / "moderator.json"
    moderator_path.write_text(
        json.dumps([{"turn": 2, "content": "Name the operational risk."}]),
        encoding="utf-8",
    )

    result = _run_cli(
        "--topic",
        "Prototype the relay engine first, not the API glue.",
        "--turns",
        "3",
        "--out",
        str(out_path),
        "--moderator-script",
        str(moderator_path),
    )

    assert result.returncode == 0, result.stderr
    assert out_path.exists()
    assert "COMPLETED: appended 5 messages" in result.stdout


def test_cli_resume_reports_appended_message_count_not_total(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_count.jsonl"
    initial = RelayConfig(
        topic="Resume summaries should report appended messages, not total transcript length.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 0, result.stderr
    assert "COMPLETED: appended 2 messages" in result.stdout
    assert "(6 total)" in result.stdout


def test_cli_resume_prints_only_appended_messages(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_stdout.jsonl"
    initial = RelayConfig(
        topic="Resume output should list only the messages appended by that invocation.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    lines = [line for line in result.stdout.splitlines() if line.strip()]

    assert result.returncode == 0, result.stderr
    assert lines[0].startswith("COMPLETED: appended 2 messages")
    assert len(lines) == 3
    assert lines[1].startswith("05 [Claude] ")
    assert lines[2].startswith("06 [Codex] ")


def test_cli_reports_missing_moderator_script(tmp_path: Path) -> None:
    result = _run_cli(
        "--topic",
        "Prototype the relay engine first, not the API glue.",
        "--turns",
        "3",
        "--moderator-script",
        str(tmp_path / "missing.json"),
    )

    assert result.returncode == 2
    assert "Moderator script not found" in result.stderr


def test_cli_rejects_moderator_script_with_boolean_turn(tmp_path: Path) -> None:
    moderator_path = tmp_path / "moderator_invalid_turn.json"
    moderator_path.write_text(
        json.dumps([{"turn": True, "content": "Bad turn type."}]),
        encoding="utf-8",
    )

    result = _run_cli(
        "--topic",
        "Moderator scripts should reject boolean turns instead of coercing them to 1.",
        "--turns",
        "2",
        "--moderator-script",
        str(moderator_path),
    )

    assert result.returncode == 2
    assert "Moderator event turn must be a positive integer" in result.stderr


def test_cli_rejects_moderator_script_with_non_string_content(tmp_path: Path) -> None:
    moderator_path = tmp_path / "moderator_invalid_content.json"
    moderator_path.write_text(
        json.dumps([{"turn": 1, "content": 7, "author": 9}]),
        encoding="utf-8",
    )

    result = _run_cli(
        "--topic",
        "Moderator scripts should reject non-string content and authors.",
        "--turns",
        "2",
        "--moderator-script",
        str(moderator_path),
    )

    assert result.returncode == 2
    assert "Moderator event content must be a string" in result.stderr


def test_cli_resume_uses_stored_topic_when_topic_omitted(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_topic.jsonl"
    initial = RelayConfig(
        topic="Resume should recover the stored topic automatically.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    stored = TranscriptStore(out_path).read()
    topic_messages = [item for item in stored if item["metadata"].get("kind") == "topic"]

    assert result.returncode == 0, result.stderr
    assert "COMPLETED:" in result.stdout
    assert len(topic_messages) == 1
    assert topic_messages[0]["content"] == "Resume should recover the stored topic automatically."


def test_cli_resume_uses_stored_agent_configuration_when_flags_omitted(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_agent_config.jsonl"
    initial = RelayConfig(
        topic="Resume should preserve the stored agent identity and instruction.",
        turns=2,
        left_agent=AgentConfig(name="Alice", instruction="Keep the critique concrete."),
        right_agent=AgentConfig(name="Bob", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    result = _run_cli(
        "--turns",
        "3",
        "--resume",
        "--out",
        str(out_path),
    )

    stored = TranscriptStore(out_path).read()
    agent_messages = [item for item in stored if item["role"] == "agent"]

    assert result.returncode == 0, result.stderr
    assert [item["author"] for item in agent_messages] == ["Alice", "Alice"]
    assert "Keep the critique concrete." in agent_messages[-1]["content"]


def test_cli_resume_uses_stored_moderator_events_when_script_omitted(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_moderator_events.jsonl"
    initial = RelayConfig(
        topic="Resume should preserve future moderator interjections.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        moderator_events=[ModeratorEvent(turn=3, content="Stored moderator checkpoint.")],
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    result = _run_cli(
        "--turns",
        "3",
        "--resume",
        "--out",
        str(out_path),
    )

    stored = TranscriptStore(out_path).read()
    moderator_messages = [item for item in stored if item["metadata"].get("kind") == "interjection"]

    assert result.returncode == 0, result.stderr
    assert [item["content"] for item in moderator_messages] == ["Stored moderator checkpoint."]


def test_moderator_events_from_snapshot_rejects_invalid_types() -> None:
    with pytest.raises(ValueError, match="Stored transcript is missing valid moderator event metadata"):
        cli_module._moderator_events_from_snapshot([{"turn": True, "content": 7, "author": 9}])


def test_resolve_session_field_rejects_invalid_stored_type(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli_module.build_parser()

    with pytest.raises(SystemExit) as excinfo:
        cli_module._resolve_session_field(
            args_value=None,
            stored_value=True,
            default="Claude",
            flag="--left-name",
            parser=parser,
            resume=True,
        )

    assert excinfo.value.code == 2
    assert "Stored transcript is missing valid session metadata" in capsys.readouterr().err


def test_cli_resume_rejects_mismatched_topic(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_topic_mismatch.jsonl"
    initial = RelayConfig(
        topic="Stored topic.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    result = _run_cli(
        "--topic",
        "Different topic.",
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "--topic must match the stored transcript topic when using --resume" in result.stderr


def test_cli_resume_rejects_transcript_with_forged_topic_after_pause(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_forged_topic.jsonl"
    initial = RelayConfig(
        topic="Resume should reject transcripts whose stored topic changes after a pause.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[0]["content"] = "Mallory rewrote the stored topic after the pause."
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript pause resume_state_digest does not match the stored topic and session at line 4" in result.stderr


def test_cli_resume_rejects_transcript_with_forged_agent_content_after_pause(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_forged_agent_content.jsonl"
    initial = RelayConfig(
        topic="Resume should reject transcripts whose prior agent content changes after a pause.",
        turns=2,
        left_agent=AgentConfig(name="Claude", fault_script=["operator"]),
        right_agent=AgentConfig(name="Codex"),
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    for row in rows:
        if row["role"] == "agent" and row["author"] == "Claude":
            row["content"] = "Mallory rewrote Claude's earlier turn."
            break
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "2",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript pause conversation_digest does not match the stored conversation prefix at line 3" in result.stderr


def test_cli_resume_rejects_mismatched_agent_provider(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_provider_mismatch.jsonl"
    initial = RelayConfig(
        topic="Stored provider settings should anchor resume.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
        "--left-provider",
        "openai",
    )

    assert result.returncode == 2
    assert "--left-provider must match the stored transcript when using --resume" in result.stderr


def test_cli_resume_rejects_turns_below_stored_next_turn(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_turn_limit.jsonl"
    initial = RelayConfig(
        topic="Resume should require turns that reach the stored next_turn.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    result = _run_cli(
        "--turns",
        "2",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "stored next_turn 3 exceeds configured turns 2" in result.stderr


def test_cli_resume_rejects_transcript_without_stored_session_metadata(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_legacy.jsonl"
    TranscriptStore(out_path).write(
        [
            Message(
                seq=1,
                timestamp="2026-03-31T20:00:00+00:00",
                role="moderator",
                author="Satisho",
                content="Legacy transcript without a session snapshot.",
                metadata={"kind": "topic"},
            ),
            Message(
                seq=2,
                timestamp="2026-03-31T20:00:01+00:00",
                role="system",
                author="relay",
                content="Paused relay: legacy test.",
                metadata={"kind": "pause", "status": "paused", "reason": "Paused relay: legacy test.", "next_turn": 2},
            ),
        ]
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Cannot safely resume transcript without stored session metadata" in result.stderr


def test_cli_resume_rejects_transcript_with_invalid_stored_session_metadata(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_invalid_session_metadata.jsonl"
    initial = RelayConfig(
        topic="Resume should reject malformed stored session metadata, not coerce it.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        moderator_events=[ModeratorEvent(turn=1, content="Stored moderator event.")],
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[0]["metadata"]["session"]["moderator_events"][0]["turn"] = True
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "3",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript has incomplete stored session metadata" in result.stderr


def test_cli_resume_rejects_transcript_with_unknown_provider_in_stored_session_metadata(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_unknown_provider_session_metadata.jsonl"
    initial = RelayConfig(
        topic="Resume should reject stored session snapshots with unsupported providers.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[0]["metadata"]["session"]["left_agent"]["provider"] = "banana"
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "3",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript has incomplete stored session metadata" in result.stderr


def test_cli_resume_rejects_transcript_with_invalid_message_shape(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_invalid_shape.jsonl"
    out_path.write_text(
        json.dumps(
            {
                "seq": 1,
                "timestamp": "2026-03-31T20:00:00+00:00",
                "role": "moderator",
                "content": "Malformed transcript row.",
                "metadata": {"kind": "topic", "session": {}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript contains an invalid message shape at line 1" in result.stderr


def test_cli_resume_rejects_transcript_with_invalid_role(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_invalid_role.jsonl"
    initial = RelayConfig(
        topic="Resume should reject transcripts with impossible role values.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[1]["role"] = "banana"
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript contains an invalid role at line 2" in result.stderr


def test_cli_resume_rejects_transcript_with_topic_author_drift(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_topic_author_drift.jsonl"
    initial = RelayConfig(
        topic="Resume should reject transcripts whose topic author drifts from the stored moderator.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[0]["author"] = "Mallory"
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript topic author does not match the stored moderator at line 1" in result.stderr


def test_cli_resume_rejects_transcript_with_moderator_interjection_author_drift(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_interjection_author_drift.jsonl"
    initial = RelayConfig(
        topic="Resume should reject transcripts whose moderator interjection author drifts from the stored session.",
        turns=5,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["ok", "timeout"]),
        moderator_events=[ModeratorEvent(turn=3, content="Stored moderator checkpoint.")],
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    for row in rows:
        if row["metadata"].get("kind") == "interjection":
            row["author"] = "Mallory"
            break
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "6",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript moderator interjection does not match the stored session at line 4" in result.stderr


def test_cli_resume_rejects_transcript_with_duplicate_terminal_turn(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_duplicate_terminal_turn.jsonl"
    initial = RelayConfig(
        topic="Resume should reject transcripts that duplicate a completed turn.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    duplicate = next(row for row in rows if row["role"] == "agent")
    rows.insert(2, {**duplicate, "seq": 3})
    for index, row in enumerate(rows, start=1):
        row["seq"] = index
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript turn order does not match relay execution at line 3" in result.stderr


def test_cli_resume_rejects_transcript_with_non_monotonic_seq(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_non_monotonic.jsonl"
    initial = RelayConfig(
        topic="Resume should reject transcripts with out-of-order sequence numbers.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[1]["seq"] = 5
    rows[2]["seq"] = 3
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript contains a non-monotonic seq at line 3" in result.stderr


def test_cli_resume_rejects_transcript_with_non_positive_next_turn(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_non_positive_next_turn.jsonl"
    initial = RelayConfig(
        topic="Resume should reject pause markers with non-positive next_turn values.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[-1]["metadata"]["next_turn"] = 0
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript pause messages must declare a positive next_turn at line 4" in result.stderr


def test_cli_resume_rejects_transcript_with_multiple_topic_messages(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_duplicate_topic.jsonl"
    initial = RelayConfig(
        topic="Resume should reject transcripts with duplicate topic messages.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows.insert(-1, {**rows[0], "seq": 4})
    rows[-1]["seq"] = 5
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    result = _run_cli(
        "--turns",
        "4",
        "--resume",
        "--out",
        str(out_path),
    )

    assert result.returncode == 2
    assert "Transcript contains multiple topic messages at line 4" in result.stderr


def test_relay_pauses_after_three_non_appends(tmp_path: Path) -> None:
    out_path = tmp_path / "paused_failures.jsonl"
    config = RelayConfig(
        topic="Stress the non-append breaker.",
        turns=8,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout", "error", "empty"]),
    )

    result = RelayRunner(config=config, out_path=out_path).run()

    assert result.status == "paused"
    assert "3 consecutive failed attempts" in result.pause_reason
    assert result.messages[-1].metadata["kind"] == "pause"
    assert [message.author for message in result.messages if message.role == "agent"] == ["Claude", "Claude", "Claude"]


def test_relay_pauses_after_eight_one_sided_appends(tmp_path: Path) -> None:
    out_path = tmp_path / "paused_one_sided.jsonl"
    config = RelayConfig(
        topic="Catch one-sided growth before it runs away.",
        turns=16,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"] * 8),
        max_failed_attempts=99,
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    agent_messages = [message for message in result.messages if message.role == "agent"]

    assert result.status == "paused"
    assert "0 committed messages after 8 agent appends" in result.pause_reason
    assert len(agent_messages) == 8
    assert {message.author for message in agent_messages} == {"Claude"}


def test_relay_pauses_on_operator_language_tripwire(tmp_path: Path) -> None:
    out_path = tmp_path / "paused_operator.jsonl"
    config = RelayConfig(
        topic="Catch operator language emitted by an agent.",
        turns=2,
        left_agent=AgentConfig(name="Claude", fault_script=["operator"]),
        right_agent=AgentConfig(name="Codex"),
    )

    result = RelayRunner(config=config, out_path=out_path).run()

    assert result.status == "paused"
    assert "operator-language tripwire" in result.pause_reason
    assert len([message for message in result.messages if message.role == "agent"]) == 1
    assert result.messages[-1].metadata["kind"] == "pause"


def test_resume_from_paused_skips_duplicate_turn(tmp_path: Path) -> None:
    out_path = tmp_path / "resume.jsonl"
    first_config = RelayConfig(
        topic="Pause and then continue without replaying the same turn.",
        turns=8,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout", "timeout", "timeout"]),
    )

    first_result = RelayRunner(config=first_config, out_path=out_path).run()
    assert first_result.status == "paused"

    resumed_config = RelayConfig(
        topic="Pause and then continue without replaying the same turn.",
        turns=8,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )

    resumed_result = RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)
    stored = TranscriptStore(out_path).read()
    agent_turns = [item["metadata"]["turn"] for item in stored if item["role"] == "agent"]
    topic_messages = [item for item in stored if item["metadata"].get("kind") == "topic"]

    assert resumed_result.status == "completed"
    assert agent_turns == [1, 3, 5, 7, 8]
    assert len(topic_messages) == 1


def test_resume_restarts_breaker_counters(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_counters.jsonl"
    first_config = RelayConfig(
        topic="Resume should start breaker counters from zero.",
        turns=3,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout", "timeout"]),
        max_failed_attempts=99,
        max_total_appends_without_both=2,
    )

    first_result = RelayRunner(config=first_config, out_path=out_path).run()
    assert first_result.status == "paused"
    assert "0 committed messages after 2 agent appends" in first_result.pause_reason

    resumed_config = RelayConfig(
        topic="Resume should start breaker counters from zero.",
        turns=5,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=2,
        max_total_appends_without_both=3,
    )

    resumed_result = RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)
    stored = TranscriptStore(out_path).read()
    agent_turns = [item["metadata"]["turn"] for item in stored if item["role"] == "agent"]
    failure_types = [
        item["metadata"]["failure_type"]
        for item in stored
        if item["metadata"].get("kind") == "attempt_failed"
    ]

    assert resumed_result.status == "completed"
    assert agent_turns == [1, 3, 5]
    assert failure_types == ["timeout", "timeout"]


def test_resume_restores_remaining_fault_scripts_from_pause_state(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_fault_state.jsonl"
    initial_config = RelayConfig(
        topic="Resume should continue with the remaining injected faults, not replay consumed ones.",
        turns=3,
        left_agent=AgentConfig(name="Claude", fault_script=["timeout", "ok"]),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    first_result = RelayRunner(config=initial_config, out_path=out_path).run()
    assert first_result.status == "paused"
    assert first_result.messages[-1].metadata["fault_state"] == {
        "left_agent": ["ok"],
        "right_agent": [],
    }

    resumed_result = RelayRunner(config=initial_config, out_path=out_path).run(resume=True)
    stored = TranscriptStore(out_path).read()
    failure_turns = [
        item["metadata"]["turn"]
        for item in stored
        if item["metadata"].get("kind") == "attempt_failed"
    ]
    final_agent = next(
        item
        for item in reversed(stored)
        if item["role"] == "agent" and item["author"] == "Claude"
    )

    assert resumed_result.status == "completed"
    assert failure_turns == [1]
    assert final_agent["metadata"]["turn"] == 3
    assert "responding to Codex" in final_agent["content"]


def test_resume_can_continue_after_multiple_prior_pauses(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_multiple_pauses.jsonl"
    first_config = RelayConfig(
        topic="Resume should allow later runs after earlier pause markers in the same transcript.",
        turns=6,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout", "timeout"]),
        max_failed_attempts=1,
    )

    first_result = RelayRunner(config=first_config, out_path=out_path).run()
    assert first_result.status == "paused"

    second_result = RelayRunner(config=first_config, out_path=out_path).run(resume=True)
    assert second_result.status == "paused"

    third_config = RelayConfig(
        topic="Resume should allow later runs after earlier pause markers in the same transcript.",
        turns=6,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    third_result = RelayRunner(config=third_config, out_path=out_path).run(resume=True)
    stored = TranscriptStore(out_path).read()
    agent_turns = [item["metadata"]["turn"] for item in stored if item["role"] == "agent"]
    failure_turns = [
        item["metadata"]["turn"]
        for item in stored
        if item["metadata"].get("kind") == "attempt_failed"
    ]

    assert third_result.status == "completed"
    assert agent_turns == [1, 3, 5, 6]
    assert failure_turns == [2, 4]


def test_engine_resume_rejects_mismatched_stored_session(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_mismatch.jsonl"
    first_config = RelayConfig(
        topic="Engine resume should reject session drift, not just the CLI.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=first_config, out_path=out_path).run()

    resumed_config = RelayConfig(
        topic="Engine resume should reject session drift, not just the CLI.",
        turns=3,
        left_agent=AgentConfig(name="Claude", provider="openai", model="gpt-5.4"),
        right_agent=AgentConfig(name="Codex"),
    )

    with pytest.raises(ValueError, match="configured session does not match the stored transcript session"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_forged_agent_instruction_after_pause(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_forged_instruction.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject transcripts whose stored agent instruction changes after a pause.",
        turns=2,
        left_agent=AgentConfig(name="Claude", instruction="Keep the critique concrete."),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[0]["metadata"]["session"]["left_agent"]["instruction"] = "Mallory rewrote the stored instruction."
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject transcripts whose stored agent instruction changes after a pause.",
        turns=4,
        left_agent=AgentConfig(name="Claude", instruction="Keep the critique concrete."),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    with pytest.raises(
        ValueError,
        match="Transcript pause resume_state_digest does not match the stored topic and session at line 4",
    ):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_forged_agent_content_after_pause(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_forged_agent_content.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject transcripts whose prior agent content changes after a pause.",
        turns=2,
        left_agent=AgentConfig(name="Claude", fault_script=["operator"]),
        right_agent=AgentConfig(name="Codex"),
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    for row in rows:
        if row["role"] == "agent" and row["author"] == "Claude":
            row["content"] = "Mallory rewrote Claude's earlier turn."
            break
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject transcripts whose prior agent content changes after a pause.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )

    with pytest.raises(
        ValueError,
        match="Transcript pause conversation_digest does not match the stored conversation prefix at line 3",
    ):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_forged_fault_state_after_pause(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_forged_fault_state.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject transcripts whose stored remaining fault state changes after a pause.",
        turns=3,
        left_agent=AgentConfig(name="Claude", fault_script=["timeout", "ok"]),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[-1]["metadata"]["fault_state"]["left_agent"] = ["timeout"]
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject transcripts whose stored remaining fault state changes after a pause.",
        turns=3,
        left_agent=AgentConfig(name="Claude", fault_script=["timeout", "ok"]),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    with pytest.raises(
        ValueError,
        match="Transcript pause resume_state_digest does not match the stored topic and session at line 3",
    ):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_without_stored_session_metadata(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_legacy.jsonl"
    TranscriptStore(out_path).write(
        [
            Message(
                seq=1,
                timestamp="2026-03-31T20:00:00+00:00",
                role="moderator",
                author="Satisho",
                content="Legacy transcript without a session snapshot.",
                metadata={"kind": "topic"},
            ),
            Message(
                seq=2,
                timestamp="2026-03-31T20:00:01+00:00",
                role="system",
                author="relay",
                content="Paused relay: legacy test.",
                metadata={"kind": "pause", "status": "paused", "reason": "Paused relay: legacy test.", "next_turn": 2},
            ),
        ]
    )

    resumed_config = RelayConfig(
        topic="Legacy transcript without a session snapshot.",
        turns=4,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )

    with pytest.raises(ValueError, match="Cannot safely resume transcript without stored session metadata"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_invalid_stored_session_metadata(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_invalid_session_metadata.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject malformed stored session metadata.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        moderator_events=[ModeratorEvent(turn=1, content="Stored moderator event.")],
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[0]["metadata"]["session"]["moderator_events"][0]["turn"] = True
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject malformed stored session metadata.",
        turns=3,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        moderator_events=[ModeratorEvent(turn=1, content="Stored moderator event.")],
        max_failed_attempts=1,
    )

    with pytest.raises(ValueError, match="Cannot safely resume transcript with invalid stored session metadata"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_non_positive_moderator_event_turn_in_stored_session_metadata(
    tmp_path: Path,
) -> None:
    out_path = tmp_path / "resume_engine_zero_event_turn.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject stored moderator events with non-positive turns.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        moderator_events=[ModeratorEvent(turn=1, content="Stored moderator event.")],
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[0]["metadata"]["session"]["moderator_events"][0]["turn"] = 0
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject stored moderator events with non-positive turns.",
        turns=3,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        moderator_events=[ModeratorEvent(turn=1, content="Stored moderator event.")],
        max_failed_attempts=1,
    )

    with pytest.raises(ValueError, match="Cannot safely resume transcript with invalid stored session metadata"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_invalid_message_shape(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_invalid_shape.jsonl"
    out_path.write_text(
        json.dumps(
            {
                "seq": 1,
                "timestamp": "2026-03-31T20:00:00+00:00",
                "role": "moderator",
                "content": "Malformed transcript row.",
                "metadata": {"kind": "topic", "session": {}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Malformed transcript row.",
        turns=4,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )

    with pytest.raises(ValueError, match="Transcript contains an invalid message shape at line 1"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_non_relay_system_author(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_system_author.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject transcripts with non-relay system messages.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[-1]["author"] = "Claude"
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject transcripts with non-relay system messages.",
        turns=4,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    with pytest.raises(ValueError, match="Transcript system messages must be authored by relay at line 4"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_agent_author_drift(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_agent_author_drift.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject transcripts whose agent author drifts from the stored session.",
        turns=5,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["ok", "timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    for row in rows:
        if row["role"] == "agent" and row["metadata"]["turn"] == 3:
            row["author"] = "Codex"
            break
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject transcripts whose agent author drifts from the stored session.",
        turns=6,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    with pytest.raises(ValueError, match="Transcript agent author does not match the stored session at line 4"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_missing_moderator_interjection(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_missing_interjection.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject transcripts missing moderator interjections whose turn has already run.",
        turns=5,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["ok", "timeout"]),
        moderator_events=[ModeratorEvent(turn=3, content="Stored moderator checkpoint.")],
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = [
        row
        for row in TranscriptStore(out_path).read()
        if row["metadata"].get("kind") != "interjection"
    ]
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject transcripts missing moderator interjections whose turn has already run.",
        turns=6,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        moderator_events=[ModeratorEvent(turn=3, content="Stored moderator checkpoint.")],
        max_failed_attempts=1,
    )

    with pytest.raises(
        ValueError,
        match="Transcript moderator interjections do not match the stored session at line 4",
    ):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_reordered_terminal_turns(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_reordered_turns.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject transcripts whose completed turns move backward.",
        turns=4,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["ok", "timeout"]),
        max_failed_attempts=1,
        max_total_appends_without_both=99,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    agent_index = next(i for i, row in enumerate(rows) if row["role"] == "agent" and row["metadata"]["turn"] == 3)
    failure_index = next(
        i
        for i, row in enumerate(rows)
        if row["metadata"].get("kind") == "attempt_failed" and row["metadata"]["turn"] == 4
    )
    rows[agent_index], rows[failure_index] = rows[failure_index], rows[agent_index]
    for index, row in enumerate(rows, start=1):
        row["seq"] = index
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject transcripts whose completed turns move backward.",
        turns=5,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
        max_total_appends_without_both=99,
    )

    with pytest.raises(ValueError, match="Transcript turn order does not match relay execution at line 4"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_non_monotonic_seq(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_non_monotonic.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject transcripts with out-of-order sequence numbers.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[1]["seq"] = 5
    rows[2]["seq"] = 3
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject transcripts with out-of-order sequence numbers.",
        turns=4,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    with pytest.raises(ValueError, match="Transcript contains a non-monotonic seq at line 3"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_with_boolean_next_turn(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_boolean_next_turn.jsonl"
    initial = RelayConfig(
        topic="Engine resume should reject pause markers with boolean next_turn values.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )
    RelayRunner(config=initial, out_path=out_path).run()

    rows = TranscriptStore(out_path).read()
    rows[-1]["metadata"]["next_turn"] = True
    out_path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n",
        encoding="utf-8",
    )

    resumed_config = RelayConfig(
        topic="Engine resume should reject pause markers with boolean next_turn values.",
        turns=4,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    with pytest.raises(ValueError, match="Transcript pause messages must declare a positive next_turn at line 4"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_engine_resume_rejects_transcript_that_does_not_begin_with_topic(tmp_path: Path) -> None:
    out_path = tmp_path / "resume_engine_topic_not_first.jsonl"
    resumed_config = RelayConfig(
        topic="Resume should reject transcripts whose first message is not the original topic.",
        turns=4,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )
    session = {
        "moderator": resumed_config.moderator,
        "moderator_events": [],
        "left_agent": {
            "name": resumed_config.left_agent.name,
            "provider": resumed_config.left_agent.provider,
            "model": resumed_config.left_agent.model,
            "instruction": resumed_config.left_agent.instruction,
        },
        "right_agent": {
            "name": resumed_config.right_agent.name,
            "provider": resumed_config.right_agent.provider,
            "model": resumed_config.right_agent.model,
            "instruction": resumed_config.right_agent.instruction,
        },
    }
    TranscriptStore(out_path).write(
        [
            Message(
                seq=1,
                timestamp="2026-03-31T20:00:00+00:00",
                role="moderator",
                author="Satisho",
                content="Prelude that should never appear before the topic.",
                metadata={"kind": "interjection", "turn": 1},
            ),
            Message(
                seq=2,
                timestamp="2026-03-31T20:00:01+00:00",
                role="moderator",
                author="Satisho",
                content=resumed_config.topic,
                metadata={"kind": "topic", "session": session},
            ),
            Message(
                seq=3,
                timestamp="2026-03-31T20:00:02+00:00",
                role="system",
                author="relay",
                content="Paused relay: malformed prologue test.",
                metadata={
                    "kind": "pause",
                    "status": "paused",
                    "reason": "Paused relay: malformed prologue test.",
                    "next_turn": 2,
                },
            ),
        ]
    )

    with pytest.raises(ValueError, match="Transcript must begin with the original topic message at line 1"):
        RelayRunner(config=resumed_config, out_path=out_path).run(resume=True)


def test_reusing_config_does_not_consume_fault_scripts(tmp_path: Path) -> None:
    first_out = tmp_path / "first_reuse.jsonl"
    second_out = tmp_path / "second_reuse.jsonl"
    config = RelayConfig(
        topic="Reusing the same config should replay injected faults deterministically.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex", fault_script=["timeout"]),
        max_failed_attempts=1,
    )

    first_result = RelayRunner(config=config, out_path=first_out).run()
    second_result = RelayRunner(config=config, out_path=second_out).run()

    first_stored = TranscriptStore(first_out).read()
    second_stored = TranscriptStore(second_out).read()

    assert first_result.status == "paused"
    assert second_result.status == "paused"
    assert first_stored[-2]["metadata"]["failure_type"] == "timeout"
    assert second_stored[-2]["metadata"]["failure_type"] == "timeout"


def test_unexpected_provider_exceptions_become_failed_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingProvider(BaseProvider):
        def generate(self, agent: AgentConfig, transcript: list[Message], turn: int) -> str:
            raise RuntimeError("socket dropped")

    def fake_get_provider(name: str) -> BaseProvider:
        if name == "boom":
            return ExplodingProvider()
        return MockProvider()

    monkeypatch.setattr(engine_module, "get_provider", fake_get_provider)

    out_path = tmp_path / "unexpected_errors.jsonl"
    config = RelayConfig(
        topic="Treat unexpected provider exceptions as non-appends.",
        turns=5,
        left_agent=AgentConfig(name="Claude", provider="boom"),
        right_agent=AgentConfig(name="Codex"),
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    stored = TranscriptStore(out_path).read()
    failures = [item for item in stored if item["metadata"].get("kind") == "attempt_failed"]
    failure_types = [item["metadata"]["failure_type"] for item in failures]

    assert result.status == "paused"
    assert "3 consecutive failed attempts" in result.pause_reason
    assert failure_types == ["unexpected_error"] * 3
    assert "RuntimeError: socket dropped" in failures[-1]["content"]
    assert stored[-1]["metadata"]["kind"] == "pause"


def test_unknown_provider_name_becomes_provider_error(tmp_path: Path) -> None:
    out_path = tmp_path / "unknown_provider.jsonl"
    config = RelayConfig(
        topic="Unknown provider names should become provider errors, not raw crashes.",
        turns=1,
        left_agent=AgentConfig(name="Claude", provider="banana"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    stored = TranscriptStore(out_path).read()

    assert result.status == "paused"
    assert stored[-2]["metadata"]["failure_type"] == "provider_error"
    assert "Unknown provider 'banana'" in stored[-2]["content"]


def test_invalid_provider_json_becomes_provider_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_urlopen(request, timeout: int = 60) -> FakeResponse:
        return FakeResponse("not valid json")

    monkeypatch.setattr(providers_module.urllib.request, "urlopen", fake_urlopen)

    out_path = tmp_path / "invalid_json.jsonl"
    config = RelayConfig(
        topic="Invalid provider JSON should be classified as a provider error.",
        turns=1,
        left_agent=AgentConfig(name="Claude", provider="openai", model="gpt-test"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    stored = TranscriptStore(out_path).read()

    assert result.status == "paused"
    assert stored[-2]["metadata"]["failure_type"] == "provider_error"
    assert "invalid JSON" in stored[-2]["content"]


def test_openai_tool_calls_become_provider_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_urlopen(request, timeout: int = 60) -> FakeResponse:
        payload = {
            "output": [
                {
                    "type": "function_call",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ]
        }
        return FakeResponse(json.dumps(payload))

    monkeypatch.setattr(providers_module.urllib.request, "urlopen", fake_urlopen)

    out_path = tmp_path / "openai_tool_call.jsonl"
    config = RelayConfig(
        topic="Tool-call-only OpenAI responses should be classified explicitly.",
        turns=1,
        left_agent=AgentConfig(name="Claude", provider="openai", model="gpt-test"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    stored = TranscriptStore(out_path).read()

    assert result.status == "paused"
    assert stored[-2]["metadata"]["failure_type"] == "provider_error"
    assert "tool calls without text content" in stored[-2]["content"]


def test_anthropic_tool_use_becomes_provider_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fake_urlopen(request, timeout: int = 60) -> FakeResponse:
        payload = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "lookup",
                    "input": {},
                }
            ]
        }
        return FakeResponse(json.dumps(payload))

    monkeypatch.setattr(providers_module.urllib.request, "urlopen", fake_urlopen)

    out_path = tmp_path / "anthropic_tool_use.jsonl"
    config = RelayConfig(
        topic="Tool-use-only Anthropic responses should be classified explicitly.",
        turns=1,
        left_agent=AgentConfig(name="Claude", provider="anthropic", model="claude-test"),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    stored = TranscriptStore(out_path).read()

    assert result.status == "paused"
    assert stored[-2]["metadata"]["failure_type"] == "provider_error"
    assert "tool use without text content" in stored[-2]["content"]


def test_anthropic_request_coalesces_consecutive_same_role_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen_payload: dict[str, object] = {}

    def fake_urlopen(request, timeout: int = 60) -> FakeResponse:
        nonlocal seen_payload
        seen_payload = json.loads(request.data.decode("utf-8"))
        return FakeResponse(json.dumps({"content": [{"type": "text", "text": "ok"}]}))

    monkeypatch.setattr(providers_module.urllib.request, "urlopen", fake_urlopen)

    transcript = [
        Message(
            seq=1,
            timestamp="2026-03-31T20:00:00+00:00",
            role="moderator",
            author="Satisho",
            content="Topic",
            metadata={},
        ),
        Message(
            seq=2,
            timestamp="2026-03-31T20:00:01+00:00",
            role="moderator",
            author="Satisho",
            content="Tension point",
            metadata={"kind": "interjection"},
        ),
        Message(
            seq=3,
            timestamp="2026-03-31T20:00:02+00:00",
            role="agent",
            author="Claude",
            content="First reply",
            metadata={"turn": 1},
        ),
        Message(
            seq=4,
            timestamp="2026-03-31T20:00:03+00:00",
            role="moderator",
            author="Satisho",
            content="Push deeper",
            metadata={},
        ),
    ]

    provider = AnthropicProvider()
    response = provider.generate(
        AgentConfig(name="Codex", provider="anthropic", model="claude-test"),
        transcript,
        turn=4,
    )

    assert response == "ok"
    assert seen_payload == {
        "model": "claude-test",
        "max_tokens": 700,
        "system": "",
        "messages": [
            {"role": "user", "content": "Satisho: Topic\n\nSatisho: Tension point"},
            {"role": "assistant", "content": "Claude: First reply"},
            {"role": "user", "content": "Satisho: Push deeper"},
        ],
    }


def test_trace_provider_payloads_persists_sanitized_request_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_urlopen(request, timeout: int = 60) -> FakeResponse:
        return FakeResponse(json.dumps({"output_text": "Forward progress."}))

    monkeypatch.setattr(providers_module.urllib.request, "urlopen", fake_urlopen)

    out_path = tmp_path / "provider_trace.jsonl"
    config = RelayConfig(
        topic="Trace the real provider payload shape before the first live drill.",
        turns=1,
        trace_provider_payloads=True,
        left_agent=AgentConfig(
            name="Claude",
            provider="openai",
            model="gpt-test",
            instruction="Respond precisely.",
        ),
        right_agent=AgentConfig(name="Codex"),
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    stored = TranscriptStore(out_path).read()
    trace = next(item for item in stored if item["role"] == "system")

    assert result.status == "completed"
    assert trace["role"] == "system"
    assert trace["author"] == "relay"
    assert trace["metadata"]["provider"] == "openai"
    assert trace["metadata"]["speaker"] == "Claude"
    assert trace["metadata"]["endpoint"] == "https://api.openai.com/v1/responses"
    assert trace["metadata"]["payload"]["model"] == "gpt-test"
    assert trace["metadata"]["payload"]["instructions"] == "Respond precisely."
    assert "Authorization" not in trace["metadata"].get("headers", {})


def test_trace_provider_payloads_skips_fault_injected_non_calls(tmp_path: Path) -> None:
    out_path = tmp_path / "provider_trace_fault.jsonl"
    config = RelayConfig(
        topic="Do not log fake provider requests for injected non-calls.",
        turns=1,
        trace_provider_payloads=True,
        left_agent=AgentConfig(
            name="Claude",
            provider="openai",
            model="gpt-test",
            fault_script=["timeout"],
        ),
        right_agent=AgentConfig(name="Codex"),
        max_failed_attempts=1,
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    stored = TranscriptStore(out_path).read()
    traces = [item for item in stored if item["metadata"].get("kind") == "provider_request"]

    assert result.status == "paused"
    assert not traces


def test_mock_provider_ignores_system_messages_in_context(tmp_path: Path) -> None:
    out_path = tmp_path / "system_context.jsonl"
    config = RelayConfig(
        topic="Keep relay diagnostics out of model context.",
        turns=2,
        left_agent=AgentConfig(name="Claude", fault_script=["timeout"]),
        right_agent=AgentConfig(name="Codex"),
    )

    result = RelayRunner(config=config, out_path=out_path).run()

    assert result.status == "completed"
    assert "responding to Satisho" in result.messages[-1].content
    assert "responding to relay" not in result.messages[-1].content


def test_relay_blocks_permission_request_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "policy_gate_permission.jsonl"

    class PermissionLoopProvider(BaseProvider):
        def generate(self, agent: AgentConfig, transcript: list[Message], turn: int) -> str:
            if agent.name == "Claude":
                return "I need write permission to relay_discussion/engine.py before I can proceed."
            return "Codex is moving the implementation forward."

    monkeypatch.setattr(engine_module, "get_provider", lambda name, **kwargs: PermissionLoopProvider())

    config = RelayConfig(
        topic="Permission requests should be denied at the relay boundary.",
        turns=2,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    loaded = TranscriptStore(out_path).load_messages()
    policy_messages = [m for m in loaded if m.metadata.get("kind") == "policy_gate"]
    agent_messages = [m for m in loaded if m.role == "agent"]

    assert result.status == "completed"
    assert len(policy_messages) == 1
    assert policy_messages[0].metadata["decision"] == "block"
    assert policy_messages[0].metadata["speaker"] == "Claude"
    assert [m.author for m in agent_messages] == ["Codex"]


def test_relay_forces_change_after_repeated_permission_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_path = tmp_path / "policy_gate_force_change.jsonl"

    class PermissionLoopProvider(BaseProvider):
        def generate(self, agent: AgentConfig, transcript: list[Message], turn: int) -> str:
            if agent.name == "Claude":
                return "I need write permission to relay_discussion/engine.py before I can proceed."
            return "Codex is moving the implementation forward."

    monkeypatch.setattr(engine_module, "get_provider", lambda name, **kwargs: PermissionLoopProvider())

    config = RelayConfig(
        topic="Repeated permission loops should force a strategy change.",
        turns=7,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )

    result = RelayRunner(config=config, out_path=out_path).run()
    policy_messages = [m for m in result.messages if m.metadata.get("kind") == "policy_gate"]

    assert result.status == "completed"
    # OutputDeltaRule (max_identical=2) fires on the 3rd identical permission
    # request, before RepeatedFailureRule (max_consecutive=3) gets a chance.
    # Turns: block(1), block(3), force_change(5), force_change(7).
    # Turn 5 force_change is from OutputDeltaRule, turn 7 from both rules.
    decisions = [m.metadata["decision"] for m in policy_messages]
    assert decisions[:2] == ["block", "block"]
    assert all(d == "force_change" for d in decisions[2:])
    assert all(m.author != "Claude" for m in result.messages if m.role == "agent")


def test_resume_restores_policy_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "resume_policy_state.jsonl"

    class ResumePolicyProvider(BaseProvider):
        def generate(self, agent: AgentConfig, transcript: list[Message], turn: int) -> str:
            if agent.name == "Claude":
                return "I need write permission to relay_discussion/engine.py before I can proceed."
            if turn == 6:
                return "If you guys can't implement it, let me know where the limit is."
            return "Codex is moving the implementation forward."

    monkeypatch.setattr(engine_module, "get_provider", lambda name, **kwargs: ResumePolicyProvider())

    initial = RelayConfig(
        topic="Resume should carry policy harness state across pauses.",
        turns=6,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )
    first_result = RelayRunner(config=initial, out_path=out_path).run()

    assert first_result.status == "paused"
    assert "policy_state" in first_result.messages[-1].metadata

    resumed = RelayConfig(
        topic="Resume should carry policy harness state across pauses.",
        turns=7,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )
    second_result = RelayRunner(config=resumed, out_path=out_path).run(resume=True)
    policy_messages = [m for m in second_result.messages if m.metadata.get("kind") == "policy_gate"]

    assert second_result.status == "completed"
    assert policy_messages[-1].metadata["decision"] == "force_change"
    assert policy_messages[-1].metadata["turn"] == 7


def test_resume_rejects_forged_policy_state_after_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_path = tmp_path / "resume_forged_policy_state.jsonl"

    class ResumePolicyProvider(BaseProvider):
        def generate(self, agent: AgentConfig, transcript: list[Message], turn: int) -> str:
            if agent.name == "Claude":
                return "I need write permission to relay_discussion/engine.py before I can proceed."
            if turn == 6:
                return "If you guys can't implement it, let me know where the limit is."
            return "Codex is moving the implementation forward."

    monkeypatch.setattr(engine_module, "get_provider", lambda name, **kwargs: ResumePolicyProvider())

    initial = RelayConfig(
        topic="Resume should reject forged policy-state metadata after a pause.",
        turns=6,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )
    first_result = RelayRunner(config=initial, out_path=out_path).run()

    assert first_result.status == "paused"

    rows = TranscriptStore(out_path).read()
    rows[-1]["metadata"]["policy_state"]["history"] = []
    out_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    resumed = RelayConfig(
        topic="Resume should reject forged policy-state metadata after a pause.",
        turns=7,
        left_agent=AgentConfig(name="Claude"),
        right_agent=AgentConfig(name="Codex"),
    )

    with pytest.raises(ValueError, match="resume_state_digest"):
        RelayRunner(config=resumed, out_path=out_path).run(resume=True)
