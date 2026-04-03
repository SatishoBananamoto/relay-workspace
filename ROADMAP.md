# Relay Workspace — Roadmap & Known Issues

## Status: Functional Beta

Core engine, web viewer, and controls work. 295 tests passing. Ready for real sessions with known limitations.

---

## Known Issues (Bugs / Fragile Areas)

### P1 — Verify on next live run

| # | Issue | Detail | Fix effort |
|---|-------|--------|------------|
| K1 | Codex event types unverified | Assumed `function_call` / `function_call_output` for tool events. May need different names. | Run live, check JSONL output, update `cli_providers.py` |
| K2 | Effort override timing race | Lobby effort setting queued via moderator queue. First turn may start before the command is processed. | Move effort to AgentConfig or apply in `_get_provider()` |
| K3 | Claude `--resume` + permission changes | Changing `--allowedTools` between turns may conflict with Claude's internal session memory. | Test: deny Write, verify next turn can't write |
| K4 | Tool event mapping incomplete | Only parsing `content_block_start/delta/stop` for Claude, `function_call` for Codex. Other event types may carry useful info. | See `TOOL_EVENT_MAPPING.md`, update after live runs |

### P2 — Known limitations

| # | Issue | Detail | Fix effort |
|---|-------|--------|------------|
| K5 | Pending approval lost on crash | `_pending_approval` is in-memory. Process dies → pending turn gone. Resume starts fresh turn. | Store in pause message metadata |
| K6 | No concurrent moderator safety | Multiple tabs/users share one queue. Conflicting commands execute in queue order. | **Accepted: single-moderator by design.** Multiple tabs view, one person controls. |
| K7 | SSE history grows unbounded | Every event appended to history list for late-connecting clients. 500+ turn session = large replay. | Add max history size, or paginate |
| K8 | No discuss-mode tool restriction | `--permission-mode auto` with `--allowedTools` when no workspace. Agents CAN use tools in pure discuss mode. | Issue #7 from original bug list. Add `--disallowedTools` or no-tools mode. |
| K9 | No internal turn cap | Claude/Codex can run unlimited internal tool loops per turn. No `--max-turns` or budget limit. | Issue #13. Add `--max-turns` flag or monitor via tool events. |
| K10 | Web viewer HTML is one big string | All CSS/JS/HTML inline in `web.py`. Hard to maintain or customize. | **Accepted for now.** Single file = zero build step. Extract later if needed. |

---

## Planned Features

### Session Management (Next)

**Current state:** 25 sessions in `~/.relay/sessions/`, 14 stuck as "running" (zombies), 0 archived. Sessions identified only by UUID.

**Storage layout:**
```
~/.relay/
  sessions/<uuid>/
    meta.json           # id, topic, agents, status, timestamps, turns
    transcript.jsonl    # every message (append-only)
    workspace/          # only if --build mode
  archive/              # move target for archived sessions
```

**Existing commands:** `new`, `resume`, `list`, `archive`, `say`

**Session lifecycle:**
```
new → running → paused ↔ running → completed → archived
                                        ↓
                                    archived
```

#### Fixes (S1–S10)

| # | Fix | Detail | Priority |
|---|-----|--------|----------|
| S1 | Zombie session cleanup | 14 sessions stuck as "running" but process is dead. Add `relay cleanup` that finds sessions with status=running, checks if process is alive (via PID file or lock), marks dead ones as "crashed". | High |
| S2 | Crash-safe status | Write a PID file or lock file when engine starts. On startup, check if previous PID is alive. If not, mark session as "crashed" not "running". | High |
| S3 | `relay delete <id>` | Delete a session entirely (rm -rf the directory). With `--confirm` or interactive prompt. `relay delete --zombies` to clean all crashed/zombie sessions. | High |
| S4 | Session naming | Add `--name <name>` flag to `relay new`. Store in meta.json. Show in `relay list`. Allow `relay resume <name>` by name, not just UUID. | High |
| S5 | Better `relay list` | Show: short ID (first 8 chars), name (if set), status, turns, created date, agents, providers. Tabular format with colors. `--json` for machine-readable. | Medium |
| S6 | `relay show <id>` | Print full session details: meta, transcript stats (message count per role, total tokens if tracked), workspace contents if build mode. | Medium |
| S7 | `relay export <id>` | Export transcript as readable Markdown. Agent messages formatted, system messages as notes, moderator as blockquotes. | Medium |
| S8 | Fix `relay say` | FIFO path not created by web/engine. Either create FIFO on session start, or replace with HTTP POST to the running web viewer (`curl localhost:8411/control`). | Medium |
| S9 | `relay watch <id>` | CLI command that connects to a running session's SSE stream and prints formatted output. Like `docker logs -f` for relay. Uses `curl` to `/events` endpoint. | Low |
| S10 | Resume zombie sessions | Allow `relay resume` on sessions with status="running" (zombies) — treat them as paused. Currently only paused sessions can be resumed. | Medium |

### Web Viewer Enhancements

| # | Feature | Detail |
|---|---------|--------|
| F6 | `--host 0.0.0.0` flag | Bind to all interfaces so other devices on the network can view |
| F7 | Transcript search | Search/filter messages in the conversation pane |
| F8 | Collapsible long messages | Agent responses can be very long. Collapse with expand toggle. |
| F9 | Session info panel | Show session ID, duration, total turns, messages per agent |
| F10 | Dark/light theme toggle | Currently hardcoded dark theme |
| F11 | Mobile-friendly sidebar | Currently hidden on narrow screens. Add hamburger menu. |

### Engine Improvements

| # | Feature | Detail |
|---|---------|--------|
| F12 | Kill agent mid-turn | Currently can't interrupt a running subprocess. Add SIGTERM from web viewer. |
| F13 | Turn budget tracking | Count internal tool-use turns per agent turn (from stream-json events). Display in viewer. |
| F14 | Rate limit visibility | Detect rate limit responses, show status in viewer instead of generic failure. |
| F15 | Agent-targeted messages | Currently inject message is `[To Claude]` prefix hack. Make it first-class — agent sees directive, other agent sees it happened but not the content. |
| F16 | Parallel agent turns | Both agents work simultaneously instead of alternating. Advanced. |

### Harness Integration

| # | Feature | Detail |
|---|---------|--------|
| F17 | Live harness in real session | Enable `--use-harness` flag, test with real providers. Currently only unit-tested. |
| F18 | Obligation dashboard in viewer | Real-time obligation status. Currently shows list but doesn't auto-refresh. |
| F19 | LLM-backed intent classifier | Fallback when regex patterns miss. Currently pattern-only. |

---

## Completed (This Session)

- Web viewer with SSE streaming (`relay new --web`)
- Lobby screen with 30s countdown, model/effort/instruction config
- Full moderator controls: permissions, steering, harness, approval
- Tool event streaming (Claude + Codex internal tool calls visible)
- Provider-aware UI (Claude models vs Codex models, correct effort levels)
- Model/effort switching mid-session
- Targeted inject messages (to specific agent)
- Agent failure details in viewer
- `--verbose` fix for stream-json
- `tempfile.mktemp()` → `mkstemp()` security fix
- Timeout removed (agents run until done)
- Harness split to separate repo (SatishoBananamoto/intent-to-action)

---

## Original Bug List Status

From the session that built the relay:

| # | Issue | Status |
|---|-------|--------|
| 1 | No live output | Fixed (web viewer) |
| 2 | TUI input not working | Fixed |
| 3 | TUI scrolling | Fixed |
| 4 | Codex --skip-git-repo-check on resume | Fixed |
| 5 | Codex --add-dir on resume | Fixed |
| 6 | tempfile.mktemp() fragile | Fixed (mkstemp) |
| 7 | No discuss-mode tool restriction | Open (K8) |
| 8 | Provider not cached | Fixed |
| 9 | workspace_path not threaded | Fixed |
| 10 | Transcript validation too strict | Fixed |
| 11 | No rate limit visibility | Partially fixed (F14) |
| 12 | No internal turn visibility | Fixed (tool events stream to viewer) |
| 13 | No internal turn cap | Open (K9) |
| 14 | 600s timeout too generous | Fixed (removed, no limit) |
| 15-21 | Old bash relay issues | N/A (replaced) |
| 22 | Agents modified engine code | Mitigated (permission controls in viewer) |
