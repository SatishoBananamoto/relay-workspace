from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Sequence

from .models import AgentConfig, Message


class ProviderError(RuntimeError):
    """Raised when a provider cannot generate a response."""


class BaseProvider(ABC):
    @abstractmethod
    def generate(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> str:
        raise NotImplementedError

    def generate_stream(
        self, agent: AgentConfig, transcript: Sequence[Message], turn: int
    ) -> "Iterator[str]":
        """Yield text chunks. Default: yields the full response as one chunk."""
        from typing import Iterator
        yield self.generate(agent, transcript, turn)

    @property
    def supports_streaming(self) -> bool:
        return False

    def preview_request(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> dict[str, object] | None:
        return None


class MockProvider(BaseProvider):
    def generate(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> str:
        latest = self._latest_foreign_message(agent.name, transcript)
        latest_author = latest.author if latest else "nobody"
        latest_excerpt = _truncate(latest.content if latest else "no prior context")
        instruction = _truncate(agent.instruction or "no extra instruction", limit=80)
        return (
            f"{agent.name} turn {turn}: responding to {latest_author}. "
            f"Focus: {instruction}. Context: {latest_excerpt}"
        )

    @staticmethod
    def _latest_foreign_message(agent_name: str, transcript: Sequence[Message]) -> Message | None:
        for message in reversed(_conversation_messages(transcript)):
            if message.author != agent_name:
                return message
        return None


class OpenAIProvider(BaseProvider):
    endpoint = "https://api.openai.com/v1/responses"

    def generate(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> str:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not set")

        payload = _openai_payload(agent, transcript)
        data = _post_json(self.endpoint, payload, headers={"Authorization": f"Bearer {api_key}"})
        text = _extract_openai_text(data)
        if not text:
            raise ProviderError("OpenAI response did not include output text")
        return text

    def preview_request(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> dict[str, object]:
        return {"endpoint": self.endpoint, "payload": _openai_payload(agent, transcript)}


class AnthropicProvider(BaseProvider):
    endpoint = "https://api.anthropic.com/v1/messages"

    def generate(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> str:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")

        payload = _anthropic_payload(agent, transcript)
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        data = _post_json(self.endpoint, payload, headers=headers)
        text = _extract_anthropic_text(data)
        if not text:
            raise ProviderError("Anthropic response did not include text content")
        return text

    def preview_request(self, agent: AgentConfig, transcript: Sequence[Message], turn: int) -> dict[str, object]:
        return {"endpoint": self.endpoint, "payload": _anthropic_payload(agent, transcript)}


def get_provider(name: str, **kwargs: object) -> BaseProvider:
    providers: dict[str, BaseProvider] = {
        "mock": MockProvider(),
        "openai": OpenAIProvider(),
        "anthropic": AnthropicProvider(),
    }
    if name in ("cli-claude", "cli-codex"):
        from .cli_providers import CliClaudeProvider, CliCodexProvider

        if name == "cli-claude":
            return CliClaudeProvider(**kwargs)
        return CliCodexProvider(**kwargs)
    try:
        return providers[name]
    except KeyError as exc:
        from .models import VALID_PROVIDERS as _all
        valid = ", ".join(sorted(_all))
        raise ProviderError(f"Unknown provider '{name}'. Expected one of: {valid}") from exc


def _truncate(content: str, limit: int = 96) -> str:
    text = " ".join(content.split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _conversation_messages(transcript: Sequence[Message]) -> list[Message]:
    return [message for message in transcript if message.role != "system"]


def _message_to_openai_input(message: Message) -> dict[str, object]:
    role = "assistant" if message.role == "agent" else "user"
    return {
        "role": role,
        "content": [{"type": "input_text", "text": f"{message.author}: {message.content}"}],
    }


def _message_to_anthropic_input(message: Message) -> dict[str, str]:
    role = "assistant" if message.role == "agent" else "user"
    return {"role": role, "content": f"{message.author}: {message.content}"}


def _anthropic_messages(transcript: Sequence[Message]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    for message in _conversation_messages(transcript):
        item = _message_to_anthropic_input(message)
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["content"] = f'{merged[-1]["content"]}\n\n{item["content"]}'
        else:
            merged.append(item)
    return merged


def _openai_payload(agent: AgentConfig, transcript: Sequence[Message]) -> dict[str, object]:
    return {
        "model": agent.model,
        "instructions": agent.instruction,
        "input": [_message_to_openai_input(message) for message in _conversation_messages(transcript)],
    }


def _anthropic_payload(agent: AgentConfig, transcript: Sequence[Message]) -> dict[str, object]:
    return {
        "model": agent.model,
        "max_tokens": 700,
        "system": agent.instruction,
        "messages": _anthropic_messages(transcript),
    }


def _post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    base_headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**base_headers, **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"Provider request failed with {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Provider request failed: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Provider returned invalid JSON: {exc}") from exc


def _extract_openai_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()

    fragments: list[str] = []
    saw_tool_call = False
    for item in payload.get("output", []):
        if item.get("type") == "function_call":
            saw_tool_call = True
        for content in item.get("content", []):
            if content.get("type") == "function_call":
                saw_tool_call = True
            text = content.get("text")
            if text:
                fragments.append(text)

    text = "\n".join(fragment for fragment in fragments if fragment.strip()).strip()
    if text:
        return text
    if saw_tool_call:
        raise ProviderError("OpenAI response returned tool calls without text content")
    return ""


def _extract_anthropic_text(payload: dict) -> str:
    text_blocks: list[str] = []
    saw_tool_use = False
    for block in payload.get("content", []):
        if block.get("type") == "tool_use":
            saw_tool_use = True
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if text:
                text_blocks.append(text)

    text = "\n".join(block for block in text_blocks if block.strip()).strip()
    if text:
        return text
    if saw_tool_use:
        raise ProviderError("Anthropic response returned tool use without text content")
    return ""
