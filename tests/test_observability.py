"""Tests for observability: timing, summaries, structured logs."""

from __future__ import annotations

import json
from pathlib import Path

from relay_discussion.models import AgentConfig, RelayConfig
from relay_discussion.engine import RelayRunner
from relay_discussion.observability import SessionObserver, SessionSummary


def _make_config(turns: int = 4) -> RelayConfig:
    return RelayConfig(
        topic="Observability test",
        turns=turns,
        left_agent=AgentConfig(name="Claude", provider="mock"),
        right_agent=AgentConfig(name="Codex", provider="mock"),
    )


def test_observer_records_turns(tmp_path: Path):
    observer = SessionObserver()
    config = _make_config(turns=4)
    out = tmp_path / "transcript.jsonl"

    runner = RelayRunner(config=config, out_path=out)
    runner.set_observer(observer)
    result = runner.run()

    assert result.status == "completed"
    assert len(observer.turns) == 4


def test_observer_summary_counts(tmp_path: Path):
    observer = SessionObserver()
    config = _make_config(turns=4)
    out = tmp_path / "transcript.jsonl"

    runner = RelayRunner(config=config, out_path=out)
    runner.set_observer(observer)
    runner.run()

    summary = observer.summary()
    assert summary.total_turns == 4
    assert summary.messages_per_agent.get("Claude", 0) == 2
    assert summary.messages_per_agent.get("Codex", 0) == 2
    assert summary.status == "completed"


def test_observer_summary_with_failures(tmp_path: Path):
    observer = SessionObserver()
    config = RelayConfig(
        topic="Failure test",
        turns=4,
        left_agent=AgentConfig(
            name="Claude", provider="mock",
            fault_script=["timeout", "ok"],
        ),
        right_agent=AgentConfig(name="Codex", provider="mock"),
    )
    out = tmp_path / "transcript.jsonl"

    runner = RelayRunner(config=config, out_path=out)
    runner.set_observer(observer)
    runner.run()

    summary = observer.summary()
    assert summary.failures_per_agent.get("Claude", 0) >= 1
    assert summary.total_turns == 4


def test_observer_summary_to_text():
    summary = SessionSummary(
        total_turns=10,
        total_duration_ms=60000,
        messages_per_agent={"Claude": 5, "Codex": 4},
        failures_per_agent={"Codex": 1},
        avg_turn_duration_ms=6000,
        longest_turn_ms=12000,
        longest_turn_agent="Claude",
        status="completed",
    )
    text = summary.to_text()
    assert "COMPLETED" in text
    assert "10 turns" in text
    assert "Claude: 5 messages" in text
    assert "1 failed" in text
    assert "Longest turn: 12.0s" in text


def test_observer_write_log(tmp_path: Path):
    observer = SessionObserver()
    config = _make_config(turns=2)
    out = tmp_path / "transcript.jsonl"

    runner = RelayRunner(config=config, out_path=out)
    runner.set_observer(observer)
    runner.run()

    log_path = tmp_path / "session.log"
    observer.write_log(log_path)

    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]

    # First line: session_start
    assert lines[0]["type"] == "session_start"
    # Middle lines: turns
    turn_lines = [l for l in lines if l["type"] == "turn"]
    assert len(turn_lines) == 2
    # Last line: session_end with summary
    assert lines[-1]["type"] == "session_end"
    assert "summary" in lines[-1]


def test_observer_paused_session(tmp_path: Path):
    observer = SessionObserver()
    config = RelayConfig(
        topic="Pause test",
        turns=6,
        left_agent=AgentConfig(
            name="Claude", provider="mock",
            fault_script=["timeout", "timeout", "timeout"],
        ),
        right_agent=AgentConfig(name="Codex", provider="mock"),
    )
    out = tmp_path / "transcript.jsonl"

    runner = RelayRunner(config=config, out_path=out)
    runner.set_observer(observer)
    result = runner.run()

    assert result.status == "paused"
    summary = observer.summary()
    assert summary.status == "paused"
    assert summary.failures_per_agent.get("Claude", 0) == 3


def test_observer_no_observer_doesnt_break(tmp_path: Path):
    """Engine works fine without any observer attached."""
    config = _make_config(turns=2)
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out)
    result = runner.run()
    assert result.status == "completed"
