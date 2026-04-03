# Tool Event Mapping — CLI Provider → Web Viewer

How agent subprocess JSONL events map to web viewer activity events.

## Claude (`claude -p --output-format stream-json`)

| Subprocess JSONL `type` | Parsed field | Web viewer event | What it shows |
|------------------------|-------------|-----------------|---------------|
| `content_block_start` (where `content_block.type == "tool_use"`) | `content_block.name`, `content_block.id` | `tool_start` | "Claude using **Read**" |
| `content_block_delta` (where `delta.type == "input_json_delta"`) | `delta.partial_json` | `tool_input` | Partial tool input preview (first 100 chars) |
| `content_block_stop` | — | `tool_end` | Tool call finished |
| `content_block_delta` (where `delta.text` exists) | `delta.text` | SSE `stream` event | Live text streaming to conversation pane |
| `result` | `session_id`, `result`, `usage`/`total_usage` | `usage` event + session ID capture | Token counts, session persistence |

### Known Claude stream-json event types (not all forwarded):
- `message_start` — message metadata, not forwarded
- `content_block_start` — tool_use or text block start
- `content_block_delta` — incremental content (text or tool input)
- `content_block_stop` — block complete
- `message_delta` — stop reason, not forwarded
- `message_stop` — message complete, not forwarded
- `result` — final result with session_id and usage

## Codex (`codex exec --json`)

| Subprocess JSONL `type` | Parsed field | Web viewer event | What it shows |
|------------------------|-------------|-----------------|---------------|
| `thread.started` | `thread_id` | (internal) | Session ID capture for `--resume` |
| `function_call` | `name`, `call_id` | `tool_start` | "Codex using **Read**" |
| `function_call_output` | — | `tool_end` | Tool call finished |
| `turn.started` | — | not forwarded | — |
| `turn.completed` | `usage` | not forwarded (could add) | — |
| `item.completed` | `item.text` | not forwarded (output via file) | — |
| `message.delta` | — | not forwarded | — |
| `message.completed` | — | not forwarded | — |

### Notes on Codex:
- Response text comes from the `-o` output file, not from stdout events
- Tool event type names (`function_call`) are assumed based on OpenAI Codex CLI patterns — **verify against actual output when running live**
- Codex does not stream text incrementally (no `supports_streaming`) — only tool events stream

## Web Viewer Activity Event Format

All tool events are wrapped by the engine as:

```json
{
  "kind": "tool_event",
  "agent": "Claude",
  "event": "tool_start",
  "tool": "Read",
  "id": "toolu_abc123"
}
```

The `kind` field is always `"tool_event"`. The `event` subfield is one of:

| `event` | Fields | Meaning |
|---------|--------|---------|
| `tool_start` | `tool`, `id` | Agent started using a tool |
| `tool_input` | `partial` | Incremental JSON input to the tool (Claude only) |
| `tool_end` | — | Tool call completed |
| `usage` | `usage` (dict with `input_tokens`, `output_tokens`) | Token usage stats |

## What's NOT Mapped (Invisible)

These happen inside the agent subprocess but are not exposed:

- **Agent thinking/reasoning** — internal chain-of-thought before tool selection
- **Tool output/results** — what the tool returned to the agent (file contents, grep results, bash output)
- **Retry loops** — if a tool fails and the agent retries
- **Cost per tool call** — no per-call token breakdown
- **Permission prompts** — when `--permission-mode auto` auto-approves a tool

## Updating This Mapping

When running live, if you see unexpected or missing events:

1. Run with debug logging: set `trace_provider_payloads: true` in config
2. Check the raw JSONL: `codex exec - --json 2>&1 | head -50`
3. For Claude: `claude -p --output-format stream-json "test" 2>&1 | head -50`
4. Update the event type names in:
   - `relay_discussion/cli_providers.py` — `generate_stream()` for Claude, `generate()` for Codex
   - This file
