# Tool Event Mapping — CLI Provider → Web Viewer

How agent subprocess JSONL events map to web viewer activity events.
**Verified against live output on 2026-04-03.**

## Claude (`claude -p --output-format stream-json --verbose`)

| Subprocess JSONL `type` | Parsed field | Web viewer event | What it shows |
|------------------------|-------------|-----------------|---------------|
| `assistant` (content has `tool_use`) | `message.content[].name`, `.id`, `.input` | `tool_start` | "Claude using **Read**" + input preview |
| `user` (content has `tool_result`) | — | `tool_end` | Tool call finished |
| `assistant` (content has `text`) | `message.content[].text` | SSE `stream` event | Live text streaming |
| `result` | `session_id`, `result`, `usage`, `modelUsage` | `usage` + `model_info` events | Token counts, model version |

### Actual Claude stream-json event types (verified):
```jsonl
{"type":"system","subtype":"init","session_id":"...","tools":[...],"model":"claude-sonnet-4-6",...}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"..."}}],...}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"..."}],...}}
{"type":"assistant","message":{"content":[{"type":"text","text":"The answer is..."}],...}}
{"type":"result","result":"The answer is...","session_id":"...","usage":{...},"modelUsage":{...}}
```

**Note:** With `--verbose`, Claude emits full message objects (not `content_block_delta` events). Each `assistant` message contains the complete content array.

## Codex (`codex exec --json`)

| Subprocess JSONL `type` | Parsed field | Web viewer event | What it shows |
|------------------------|-------------|-----------------|---------------|
| `thread.started` | `thread_id` | (internal) | Session ID for `resume` |
| `item.started` (where `item.type == "command_execution"`) | `item.command` | `tool_start` | "Codex using **Bash** — `rg -n ...`" |
| `item.completed` (where `item.type == "command_execution"`) | `item.exit_code`, `item.aggregated_output` | `tool_end` | Tool call finished |
| `item.completed` (where `item.type == "agent_message"`) | `item.text` | not forwarded (output via file) | — |
| `turn.completed` | `usage` | `usage` event | Token counts |

### Actual Codex JSONL event types (verified):
```jsonl
{"type":"thread.started","thread_id":"019d51d4-..."}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"..."}}
{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/bin/bash -lc \"rg ...\"","status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"...","aggregated_output":"...","exit_code":0,"status":"completed"}}
{"type":"turn.completed","usage":{"input_tokens":113687,"cached_input_tokens":78976,"output_tokens":399}}
```

**Note:** Codex only uses `command_execution` (Bash) — it doesn't have separate Read/Edit/Grep tools like Claude. All file operations go through shell commands.

## Codex Config (from `~/.codex/config.toml`)

```toml
model = "gpt-5.4"
model_reasoning_effort = "xhigh"
```

Available models: `gpt-5.4`, `o3`, `o4-mini`, `gpt-4.1`
Available effort: `xhigh`, `high`, `medium`, `low`
Set via: `-m <model>` and `-c model_reasoning_effort=<effort>`

## Web Viewer Activity Event Format

All tool events are wrapped by the engine as:

```json
{
  "kind": "tool_event",
  "agent": "Claude",
  "event": "tool_start",
  "tool": "Read",
  "id": "toolu_abc123",
  "input": "{\"file_path\":\"/home/..."
}
```

| `event` | Fields | Meaning |
|---------|--------|---------|
| `tool_start` | `tool`, `id`, `input` (first 100 chars) | Agent started using a tool |
| `tool_end` | — | Tool call completed |
| `usage` | `usage` (dict with `input_tokens`, `output_tokens`) | Token usage stats |
| `model_info` | `models` (dict of model → usage breakdown) | Per-model usage (Claude only) |

## What's NOT Mapped (Invisible)

- **Agent thinking/reasoning** — internal chain-of-thought
- **Tool output/results** — what the tool returned (file contents, grep results)
- **`system` init event** — Claude's tool list, model, permissions (could display)
- **Codex `item.completed` with `aggregated_output`** — command output (could display)
