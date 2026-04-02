"""Tests for streaming support in providers and engine."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Sequence

from relay_discussion.models import AgentConfig, Message, RelayConfig
from relay_discussion.engine import RelayRunner
from relay_discussion.providers import BaseProvider


class StreamingMockProvider(BaseProvider):
    """Mock provider that supports streaming, yielding word by word."""

    def generate(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> str:
        return f"{agent.name} turn {turn} full response"

    def generate_stream(
        self, agent: AgentConfig, transcript: Sequence[Message], turn: int
    ) -> Iterator[str]:
        words = f"{agent.name} turn {turn} streamed response".split()
        for word in words:
            yield word + " "

    @property
    def supports_streaming(self) -> bool:
        return True


class NonStreamingMockProvider(BaseProvider):
    """Mock provider that does NOT support streaming."""

    def generate(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> str:
        return f"{agent.name} turn {turn} response"


# ── BaseProvider default streaming ────────────────────────────────────────────


def test_base_provider_default_stream_yields_full_response():
    provider = NonStreamingMockProvider()
    agent = AgentConfig(name="Test", provider="mock")
    transcript: list[Message] = []
    chunks = list(provider.generate_stream(agent, transcript, 1))
    assert len(chunks) == 1
    assert chunks[0] == "Test turn 1 response"


def test_base_provider_supports_streaming_false():
    provider = NonStreamingMockProvider()
    assert provider.supports_streaming is False


def test_streaming_provider_supports_streaming_true():
    provider = StreamingMockProvider()
    assert provider.supports_streaming is True


# ── Engine streaming integration ──────────────────────────────────────────────


def _make_streaming_config() -> RelayConfig:
    return RelayConfig(
        topic="Stream test",
        turns=2,
        left_agent=AgentConfig(name="Claude", provider="mock"),
        right_agent=AgentConfig(name="Codex", provider="mock"),
    )


def test_engine_calls_on_stream_chunk_when_provider_supports_it(tmp_path: Path, monkeypatch):
    """When on_stream_chunk is set and provider supports streaming, chunks are yielded."""
    chunks_received: list[str] = []

    # Monkeypatch get_provider to return our streaming mock
    import relay_discussion.engine as engine_mod
    original_get_provider = engine_mod.get_provider

    def mock_get_provider(name, **kwargs):
        return StreamingMockProvider()

    monkeypatch.setattr(engine_mod, "get_provider", mock_get_provider)

    config = _make_streaming_config()
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(
        config=config,
        out_path=out,
        on_stream_chunk=chunks_received.append,
    )
    result = runner.run()

    assert result.status == "completed"
    # Should have received chunks for both turns
    assert len(chunks_received) > 0
    # Each chunk should be a word + space
    assert all(isinstance(c, str) for c in chunks_received)


def test_engine_skips_streaming_when_no_callback(tmp_path: Path, monkeypatch):
    """Without on_stream_chunk, engine uses generate() not generate_stream()."""
    import relay_discussion.engine as engine_mod

    call_log: list[str] = []

    class TrackingProvider(BaseProvider):
        def generate(self, agent, transcript, turn):
            call_log.append("generate")
            return f"{agent.name} response"

        def generate_stream(self, agent, transcript, turn):
            call_log.append("generate_stream")
            yield f"{agent.name} response"

        @property
        def supports_streaming(self):
            return True

    monkeypatch.setattr(engine_mod, "get_provider", lambda name, **kw: TrackingProvider())

    config = _make_streaming_config()
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(config=config, out_path=out)  # no on_stream_chunk
    runner.run()

    assert "generate" in call_log
    assert "generate_stream" not in call_log


def test_engine_uses_generate_when_provider_doesnt_support_streaming(tmp_path: Path, monkeypatch):
    """Even with on_stream_chunk set, non-streaming providers use generate()."""
    import relay_discussion.engine as engine_mod

    chunks_received: list[str] = []
    monkeypatch.setattr(engine_mod, "get_provider", lambda name, **kw: NonStreamingMockProvider())

    config = _make_streaming_config()
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(
        config=config,
        out_path=out,
        on_stream_chunk=chunks_received.append,
    )
    result = runner.run()

    assert result.status == "completed"
    # No streaming chunks because provider doesn't support it
    assert len(chunks_received) == 0


def test_streamed_response_committed_as_full_text(tmp_path: Path, monkeypatch):
    """The committed message should contain the full joined response, not individual chunks."""
    import relay_discussion.engine as engine_mod

    monkeypatch.setattr(engine_mod, "get_provider", lambda name, **kw: StreamingMockProvider())

    config = _make_streaming_config()
    out = tmp_path / "transcript.jsonl"
    runner = RelayRunner(
        config=config,
        out_path=out,
        on_stream_chunk=lambda c: None,  # discard chunks
    )
    result = runner.run()

    agent_msgs = [m for m in result.messages if m.role == "agent"]
    assert len(agent_msgs) == 2
    # Full text should be the joined chunks
    assert "streamed response" in agent_msgs[0].content
