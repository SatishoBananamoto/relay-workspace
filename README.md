# Relay Discussion

Minimal Python CLI for running a structured relay conversation between two agents with a moderator-controlled transcript.

## What it does

- Alternates turns between two configured agents
- Persists every message to JSONL as it is committed
- Accepts moderator interjections from a JSON script
- Ships with deterministic mock providers so the relay loop is testable offline
- Pauses fail-closed after repeated non-appends, one-sided growth, or operator-language tripwires
- Resumes from a paused transcript without replaying the last scheduled turn

`--turns` counts scheduled agent turn slots, not guaranteed committed agent messages. A turn still counts if the selected speaker times out, returns empty content, or hits another non-append failure.

## Quick start

```bash
python3 -m relay_discussion.cli \
  --topic "Design a better relay protocol for two AI systems." \
  --turns 4 \
  --left-name Claude \
  --right-name Codex \
  --out transcript.jsonl
```

Add moderator events with a JSON file:

```json
[
  {"turn": 2, "content": "Push on failure modes, not just happy-path design."},
  {"turn": 4, "content": "End with concrete implementation implications."}
]
```

Run:

```bash
python3 -m relay_discussion.cli \
  --topic "Prototype an AI relay interface." \
  --turns 4 \
  --moderator-script moderator_events.json \
  --out transcript.jsonl
```

## Transcript format

Each line in the output file is a JSON object:

```json
{
  "seq": 3,
  "timestamp": "2026-03-31T20:20:00+00:00",
  "role": "agent",
  "author": "Codex",
  "content": "Codex turn 2: responding to Claude ...",
  "metadata": {"provider": "mock", "model": "mirror"}
}
```

## Real providers

The package includes optional HTTP adapters for OpenAI and Anthropic. They are intentionally thin and untested in this environment. Set API keys with `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` and specify provider/model flags on the CLI.

For a live smoke drill, add `--trace-provider-payloads` to persist each sanitized provider request shape as a `system` transcript entry before the call. That makes turn-1 API contract failures diagnosable from the JSONL without logging credentials.

## Safety drills

Injected fault scripts let you verify the pause conditions without calling real APIs:

```bash
python3 -m relay_discussion.cli \
  --topic "Exercise the breaker." \
  --turns 8 \
  --right-fault-script timeout,error,empty \
  --out transcript.jsonl
```

Supported injected outcomes are `ok`, `timeout`, `error`, `empty`, and `operator`.

If the relay pauses, the transcript ends with a system message carrying `{"kind": "pause", "next_turn": ...}` metadata. Resume from that point with:

```bash
python3 -m relay_discussion.cli \
  --turns 8 \
  --resume \
  --out transcript.jsonl
```

Resume recovers the stored topic from the transcript automatically. If you do pass `--topic` on resume, it must match the stored topic exactly. Breaker counters still restart from zero for the new process.

Resume also recovers the stored session definition for agent names/providers/models/instructions and moderator events. If you pass those flags again on resume, they must match the stored transcript.
The CLI summary reports appended and total counts separately, and on resume it prints only the messages appended by that invocation.
Resume now fails closed for transcripts that do not contain the stored session snapshot on the topic message, rather than guessing defaults and silently changing the session definition.
Pause markers now persist the remaining injected fault-script state for each side, so a resumed safety drill continues with the unconsumed faults instead of replaying already-consumed ones.
Pause markers now carry a digest of the authoritative resume state `(topic + stored session snapshot + remaining fault-script state)` and a second digest of the non-system conversation prefix that will be replayed to providers on resume, so coherent edits to stored topic/session data, stored remaining fault state, or prior agent content after a pause are rejected instead of silently steering later turns.
Resume also fails closed for malformed stored session snapshots instead of coercing invalid values such as JSON booleans into numeric turn fields, unsupported provider names, or non-positive moderator event turns.
Resume also fails closed for malformed transcript rows instead of crashing with a raw traceback during transcript loading.
Resume also fails closed for non-monotonic transcript `seq` values instead of appending duplicate or out-of-order message numbers.
Resume also fails closed for malformed transcript prologues, including transcripts that do not begin with exactly one original topic message.
Resume also fails closed for protocol-impossible transcript entries, including unknown roles, agent messages with metadata kinds, and system messages not authored by `relay`.
Resume also fails closed when historical topic, moderator interjection, or agent rows drift from the stored session identity or scheduled moderator-event prefix, instead of feeding forged speakers or content back into later provider context.
Resume also fails closed for runtime-impossible turn progression, including duplicated completed turns, backward turn jumps, and other row orders the relay engine cannot emit.
Resume also fails closed for invalid pause markers, including `next_turn` values that are non-positive or encoded as JSON booleans.
Earlier pause markers are treated as completed run boundaries, not as permanent end-of-transcript markers, so a relay can pause, resume, pause again later, and still resume from the newest pause.
Unknown provider names now become classified `provider_error` failures instead of crashing the relay with a raw traceback.
The same fail-closed session check is enforced by `RelayRunner.run(resume=True)` for programmatic callers.

## Operational notes

- `--turns` is a scheduler limit. A run with `--turns 8` can finish with fewer than 8 committed agent messages if some scheduled turns fail closed.
- Resume continues from the stored `next_turn` marker. It does not replay the paused turn, but it does restart breaker counters in the new process.
