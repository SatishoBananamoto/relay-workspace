from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import RelayRunner
from .models import AgentConfig, ModeratorEvent, RelayConfig, VALID_PROVIDERS, is_strict_int, is_valid_session_snapshot
from .transcript import TranscriptStore

DEFAULT_MODERATOR_NAME = "Satisho"
DEFAULT_LEFT_NAME = "Claude"
DEFAULT_LEFT_PROVIDER = "mock"
DEFAULT_LEFT_MODEL = "mirror"
DEFAULT_LEFT_INSTRUCTION = "Be substantive and build on the last specific claim."
DEFAULT_RIGHT_NAME = "Codex"
DEFAULT_RIGHT_PROVIDER = "mock"
DEFAULT_RIGHT_MODEL = "mirror"
DEFAULT_RIGHT_INSTRUCTION = "Challenge weak assumptions and push toward implementation."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a two-agent relay discussion.")
    parser.add_argument("--topic", help="Opening moderator topic or prompt. Required unless --resume is set.")
    parser.add_argument(
        "--turns",
        type=int,
        default=4,
        help="Number of scheduled agent turn slots to run; failed attempts still consume a turn.",
    )
    parser.add_argument("--out", type=Path, default=Path("transcript.jsonl"), help="Transcript path.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from a paused transcript at the next scheduled turn without replaying prior turns.",
    )
    parser.add_argument(
        "--trace-provider-payloads",
        action="store_true",
        help="Write sanitized provider request payloads to system transcript entries for debugging.",
    )
    parser.add_argument("--moderator-name", help="Moderator display name.")
    parser.add_argument(
        "--moderator-script",
        type=Path,
        help="Path to a JSON file containing a list of {turn, content, author?} entries.",
    )

    parser.add_argument("--left-name")
    parser.add_argument("--left-provider", choices=sorted(VALID_PROVIDERS))
    parser.add_argument("--left-model")
    parser.add_argument("--left-instruction")
    parser.add_argument(
        "--left-fault-script",
        default="",
        help="Comma-separated injected outcomes for the left agent: ok,error,timeout,empty,operator.",
    )

    parser.add_argument("--right-name")
    parser.add_argument("--right-provider", choices=sorted(VALID_PROVIDERS))
    parser.add_argument("--right-model")
    parser.add_argument("--right-instruction")
    parser.add_argument(
        "--right-fault-script",
        default="",
        help="Comma-separated injected outcomes for the right agent: ok,error,timeout,empty,operator.",
    )
    parser.add_argument(
        "--max-failed-attempts",
        type=int,
        default=3,
        help="Pause after this many consecutive non-append attempts from one speaker.",
    )
    parser.add_argument(
        "--max-total-appends-without-both",
        type=int,
        default=8,
        help="Pause after this many agent appends if one speaker still has 0 committed messages.",
    )
    return parser


def load_moderator_events(path: Path | None) -> list[ModeratorEvent]:
    if path is None:
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Moderator script not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Moderator script is not valid JSON: {path}") from exc

    if isinstance(payload, dict):
        payload = payload.get("events", [])

    if not isinstance(payload, list):
        raise ValueError("Moderator script must be a list or an object with an 'events' list")

    events: list[ModeratorEvent] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each moderator event must be a JSON object")
        turn = item.get("turn")
        if not is_strict_int(turn) or turn < 1:
            raise ValueError("Moderator event turn must be a positive integer")
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError("Moderator event content must be a string")
        author = item.get("author", DEFAULT_MODERATOR_NAME)
        if not isinstance(author, str):
            raise ValueError("Moderator event author must be a string")
        events.append(
            ModeratorEvent(
                turn=turn,
                content=content,
                author=author,
            )
        )
    return events


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stored_topic, stored_session = _resolve_stored_session(args=args, parser=parser)
    topic = _resolve_topic(args=args, parser=parser, stored_topic=stored_topic)
    if args.turns < 1:
        parser.error("--turns must be at least 1")
    if args.max_failed_attempts < 1:
        parser.error("--max-failed-attempts must be at least 1")
    if args.max_total_appends_without_both < 1:
        parser.error("--max-total-appends-without-both must be at least 1")

    moderator_name = _resolve_session_field(
        args_value=args.moderator_name,
        stored_value=stored_session.get("moderator"),
        default=DEFAULT_MODERATOR_NAME,
        flag="--moderator-name",
        parser=parser,
        resume=args.resume,
    )
    moderator_events = _resolve_moderator_events(args=args, parser=parser, stored_session=stored_session)

    config = RelayConfig(
        topic=topic,
        turns=args.turns,
        moderator=moderator_name,
        moderator_events=moderator_events,
        trace_provider_payloads=args.trace_provider_payloads,
        left_agent=_resolve_agent_config(
            side="left",
            args=args,
            parser=parser,
            stored_session=stored_session,
            fault_script=_parse_fault_script(args.left_fault_script),
        ),
        right_agent=_resolve_agent_config(
            side="right",
            args=args,
            parser=parser,
            stored_session=stored_session,
            fault_script=_parse_fault_script(args.right_fault_script),
        ),
        max_failed_attempts=args.max_failed_attempts,
        max_total_appends_without_both=args.max_total_appends_without_both,
    )

    starting_message_count = len(TranscriptStore(args.out).read()) if args.resume else 0
    runner = RelayRunner(config=config, out_path=args.out)
    try:
        result = runner.run(resume=args.resume)
    except ValueError as exc:
        parser.error(str(exc))

    appended_messages = result.messages[starting_message_count:]
    appended_count = len(appended_messages)
    print(
        f"{result.status.upper()}: appended {appended_count} messages to {args.out} "
        f"({len(result.messages)} total)"
    )
    if result.pause_reason:
        print(result.pause_reason)
    for message in appended_messages:
        print(f"{message.seq:02d} [{message.author}] {message.content}")
    return 0 if result.status == "completed" else 3


def _resolve_stored_session(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[str | None, dict[str, object]]:
    if not args.resume:
        return None, {}

    try:
        return _load_stored_session(args.out)
    except ValueError as exc:
        parser.error(str(exc))
        raise AssertionError("parser.error should have exited")


def _resolve_topic(args: argparse.Namespace, parser: argparse.ArgumentParser, stored_topic: str | None) -> str:
    if not args.resume:
        if not args.topic:
            parser.error("--topic is required unless --resume is set")
        return args.topic

    assert stored_topic is not None
    if args.topic and args.topic != stored_topic:
        parser.error("--topic must match the stored transcript topic when using --resume")
    return stored_topic


def _load_stored_session(path: Path) -> tuple[str, dict[str, object]]:
    store = TranscriptStore(path)
    messages = store.load_messages()
    if not messages:
        raise ValueError(f"Cannot resume an empty transcript: {path}")

    for message in messages:
        if message.metadata.get("kind") == "topic":
            session = message.metadata.get("session")
            if session is None:
                raise ValueError(
                    "Cannot safely resume transcript without stored session metadata: "
                    f"{path}"
                )
            if not isinstance(session, dict):
                raise ValueError(f"Transcript has invalid stored session metadata: {path}")
            _validate_stored_session(path=path, session=session)
            return message.content, session

    raise ValueError(f"Transcript is missing the original topic message: {path}")


def _validate_stored_session(*, path: Path, session: dict[str, object]) -> None:
    if not is_valid_session_snapshot(session):
        raise ValueError(f"Transcript has incomplete stored session metadata: {path}")


def _resolve_agent_config(
    *,
    side: str,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    stored_session: dict[str, object],
    fault_script: list[str],
) -> AgentConfig:
    stored_agent = stored_session.get(f"{side}_agent")
    if stored_agent is not None and not isinstance(stored_agent, dict):
        parser.error(f"Stored transcript is missing valid {side} agent metadata")

    defaults = {
        "left": {
            "name": DEFAULT_LEFT_NAME,
            "provider": DEFAULT_LEFT_PROVIDER,
            "model": DEFAULT_LEFT_MODEL,
            "instruction": DEFAULT_LEFT_INSTRUCTION,
        },
        "right": {
            "name": DEFAULT_RIGHT_NAME,
            "provider": DEFAULT_RIGHT_PROVIDER,
            "model": DEFAULT_RIGHT_MODEL,
            "instruction": DEFAULT_RIGHT_INSTRUCTION,
        },
    }[side]

    return AgentConfig(
        name=_resolve_session_field(
            args_value=getattr(args, f"{side}_name"),
            stored_value=(stored_agent or {}).get("name"),
            default=defaults["name"],
            flag=f"--{side}-name",
            parser=parser,
            resume=args.resume,
        ),
        provider=_resolve_session_field(
            args_value=getattr(args, f"{side}_provider"),
            stored_value=(stored_agent or {}).get("provider"),
            default=defaults["provider"],
            flag=f"--{side}-provider",
            parser=parser,
            resume=args.resume,
        ),
        model=_resolve_session_field(
            args_value=getattr(args, f"{side}_model"),
            stored_value=(stored_agent or {}).get("model"),
            default=defaults["model"],
            flag=f"--{side}-model",
            parser=parser,
            resume=args.resume,
        ),
        instruction=_resolve_session_field(
            args_value=getattr(args, f"{side}_instruction"),
            stored_value=(stored_agent or {}).get("instruction"),
            default=defaults["instruction"],
            flag=f"--{side}-instruction",
            parser=parser,
            resume=args.resume,
        ),
        fault_script=fault_script,
    )


def _resolve_moderator_events(
    *,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    stored_session: dict[str, object],
) -> list[ModeratorEvent]:
    stored_payload = stored_session.get("moderator_events")
    if stored_payload is not None and not isinstance(stored_payload, list):
        parser.error("Stored transcript is missing valid moderator event metadata")

    if args.moderator_script is None:
        if args.resume:
            try:
                return _moderator_events_from_snapshot(stored_payload or [])
            except ValueError as exc:
                parser.error(str(exc))
                raise AssertionError("parser.error should have exited")
        return []

    try:
        events = load_moderator_events(args.moderator_script)
    except ValueError as exc:
        parser.error(str(exc))
        raise AssertionError("parser.error should have exited")

    if args.resume and stored_payload is not None and _serialize_moderator_events(events) != stored_payload:
        parser.error("--moderator-script must match the stored transcript when using --resume")
    return events


def _moderator_events_from_snapshot(payload: list[object]) -> list[ModeratorEvent]:
    events: list[ModeratorEvent] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Stored transcript is missing valid moderator event metadata")
        turn = item.get("turn")
        content = item.get("content")
        author = item.get("author")
        if not is_strict_int(turn) or turn < 1:
            raise ValueError("Stored transcript is missing valid moderator event metadata")
        if not isinstance(content, str):
            raise ValueError("Stored transcript is missing valid moderator event metadata")
        if not isinstance(author, str):
            raise ValueError("Stored transcript is missing valid moderator event metadata")
        events.append(
            ModeratorEvent(
                turn=turn,
                content=content,
                author=author,
            )
        )
    return events


def _serialize_moderator_events(events: list[ModeratorEvent]) -> list[dict[str, object]]:
    return [{"turn": event.turn, "content": event.content, "author": event.author} for event in events]


def _resolve_session_field(
    *,
    args_value: str | None,
    stored_value: object,
    default: str,
    flag: str,
    parser: argparse.ArgumentParser,
    resume: bool,
) -> str:
    if not resume:
        return args_value if args_value is not None else default

    if args_value is None:
        if stored_value is None:
            return default
        if not isinstance(stored_value, str):
            parser.error("Stored transcript is missing valid session metadata")
            raise AssertionError("parser.error should have exited")
        return stored_value

    if stored_value is not None and args_value != stored_value:
        parser.error(f"{flag} must match the stored transcript when using --resume")
    return args_value


def _parse_fault_script(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


# ── Subcommand handlers (relay new / resume / list / archive / say) ───────────

SUBCOMMANDS = {"new", "resume", "list", "archive", "say"}


def cli_entry(argv: list[str] | None = None) -> int:
    """Dispatch to subcommands or fall through to legacy main()."""
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in SUBCOMMANDS:
        cmd, rest = argv[0], argv[1:]
        if cmd == "new":
            return _cmd_new(rest)
        if cmd == "resume":
            return _cmd_resume(rest)
        if cmd == "list":
            return _cmd_list(rest)
        if cmd == "archive":
            return _cmd_archive(rest)
        if cmd == "say":
            return _cmd_say(rest)
    # Legacy mode: no subcommand, use original argument parser
    return main(argv)


def _build_new_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay new", description="Start a new relay session.")
    parser.add_argument("--topic", required=True, help="Discussion topic.")
    parser.add_argument("--turns", type=int, default=None, help="Max turns (default from config).")
    parser.add_argument("--no-limit", action="store_true", help="No turn limit.")
    parser.add_argument("--build", action="store_true", help="Enable build mode with shared workspace.")
    parser.add_argument("--tui", action="store_true", help="Launch split-pane terminal UI.")
    parser.add_argument("--web", action="store_true", help="Launch web viewer in browser.")
    parser.add_argument("--port", type=int, default=8411, help="Web viewer port (default: 8411).")
    parser.add_argument("--left-name", help="Left agent name.")
    parser.add_argument("--left-provider", help="Left agent provider.")
    parser.add_argument("--left-model", help="Left agent model.")
    parser.add_argument("--left-instruction", help="Left agent instruction.")
    parser.add_argument("--right-name", help="Right agent name.")
    parser.add_argument("--right-provider", help="Right agent provider.")
    parser.add_argument("--right-model", help="Right agent model.")
    parser.add_argument("--right-instruction", help="Right agent instruction.")
    parser.add_argument("--moderator-name", help="Moderator name.")
    parser.add_argument("--moderator-script", type=Path, help="JSON file with moderator events.")
    parser.add_argument("--max-failed-attempts", type=int, default=3)
    parser.add_argument("--max-total-appends-without-both", type=int, default=8)
    return parser


def _apply_config_overrides(config: RelayConfig, overrides: dict) -> None:
    """Apply lobby settings to the relay config before engine starts."""
    if "turns" in overrides:
        config.turns = int(overrides["turns"])
    if "left_instruction" in overrides:
        config.left_agent.instruction = overrides["left_instruction"]
    if "right_instruction" in overrides:
        config.right_agent.instruction = overrides["right_instruction"]
    if "left_model" in overrides:
        config.left_agent.model = overrides["left_model"]
    if "right_model" in overrides:
        config.right_agent.model = overrides["right_model"]
    # Effort is stored on the provider, not the config — pass through as-is
    # The engine will apply these after providers are created


def _cmd_new(argv: list[str]) -> int:
    from .config import load_config
    from .session import SessionManager

    parser = _build_new_parser()
    args = parser.parse_args(argv)
    cfg = load_config()

    left_name = args.left_name or cfg.left_name
    right_name = args.right_name or cfg.right_name
    left_provider = args.left_provider or cfg.left_provider
    right_provider = args.right_provider or cfg.right_provider
    left_model = args.left_model or cfg.left_model
    right_model = args.right_model or cfg.right_model
    moderator = args.moderator_name or cfg.moderator
    turns = args.turns or (999_999 if args.no_limit else cfg.turns)

    mgr = SessionManager()
    meta = mgr.create_session(
        topic=args.topic,
        left_agent_name=left_name,
        right_agent_name=right_name,
        moderator=moderator,
        build_mode=args.build,
        left_provider=left_provider,
        right_provider=right_provider,
        left_model=left_model,
        right_model=right_model,
    )

    moderator_events = load_moderator_events(args.moderator_script)
    left_instruction = args.left_instruction or DEFAULT_LEFT_INSTRUCTION
    right_instruction = args.right_instruction or DEFAULT_RIGHT_INSTRUCTION

    config = RelayConfig(
        topic=args.topic,
        turns=turns,
        moderator=moderator,
        moderator_events=moderator_events,
        left_agent=AgentConfig(
            name=left_name,
            provider=left_provider,
            model=left_model,
            instruction=left_instruction,
        ),
        right_agent=AgentConfig(
            name=right_name,
            provider=right_provider,
            model=right_model,
            instruction=right_instruction,
        ),
        max_failed_attempts=args.max_failed_attempts,
        max_total_appends_without_both=args.max_total_appends_without_both,
    )

    transcript_path = mgr.get_transcript_path(meta.id)
    ws_path = mgr.get_workspace_path(meta.id) if args.build else None
    mgr.update_status(meta.id, "running")

    if args.tui and args.web:
        print("ERROR: --tui and --web are mutually exclusive.", file=sys.stderr)
        return 1

    if args.tui:
        from .moderator import ModeratorInputQueue
        from .tui import run_relay_with_tui

        mq = ModeratorInputQueue()

        def runner_factory(moderator_queue=None, on_commit=None, on_stream_chunk=None, on_activity=None, config_overrides=None):
            if config_overrides:
                _apply_config_overrides(config, config_overrides)
            return RelayRunner(
                config=config,
                out_path=transcript_path,
                moderator_queue=moderator_queue,
                on_commit=on_commit,
                on_stream_chunk=on_stream_chunk,
                on_activity=on_activity,
                workspace_path=ws_path,
            )

        try:
            result = run_relay_with_tui(
                runner_factory=runner_factory,
                moderator_queue=mq,
                session_id=meta.id,
                topic=args.topic,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            mgr.update_status(meta.id, "paused")
            return 1

        if result is None:
            mgr.update_status(meta.id, "paused")
            return 1
    elif args.web:
        from .moderator import ModeratorInputQueue
        from .web import run_relay_with_web

        mq = ModeratorInputQueue()

        def runner_factory(moderator_queue=None, on_commit=None, on_stream_chunk=None, on_activity=None, config_overrides=None):
            if config_overrides:
                _apply_config_overrides(config, config_overrides)
            return RelayRunner(
                config=config,
                out_path=transcript_path,
                moderator_queue=moderator_queue,
                on_commit=on_commit,
                on_stream_chunk=on_stream_chunk,
                on_activity=on_activity,
                workspace_path=ws_path,
            )

        try:
            result = run_relay_with_web(
                runner_factory=runner_factory,
                moderator_queue=mq,
                session_id=meta.id,
                topic=args.topic,
                port=args.port,
                agents=[
                    {"name": config.left_agent.name, "provider": config.left_agent.provider, "model": config.left_agent.model},
                    {"name": config.right_agent.name, "provider": config.right_agent.provider, "model": config.right_agent.model},
                ],
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            mgr.update_status(meta.id, "paused")
            return 1

        if result is None:
            mgr.update_status(meta.id, "paused")
            return 1
    else:
        print(f"Session: {meta.id}")
        print(f"Topic:   {args.topic}")
        print(f"Agents:  {left_name} ({left_provider}) vs {right_name} ({right_provider})")
        print(f"Turns:   {'no limit' if args.no_limit else turns}")
        if args.build:
            print(f"Build:   {mgr.get_workspace_path(meta.id)}")
        print()

        def _live_print(msg):
            if msg.role == "system":
                kind = msg.metadata.get("kind", "")
                if kind == "attempt_failed":
                    print(f"  [{msg.metadata.get('speaker', '?')} FAILED] {msg.content}", flush=True)
                elif kind == "pause":
                    print(f"\n  [PAUSED] {msg.content}", flush=True)
            elif msg.role != "system":
                print(f"\n  {msg.seq:03d} [{msg.author}]\n{msg.content}\n", flush=True)

        runner = RelayRunner(config=config, out_path=transcript_path, workspace_path=ws_path, on_commit=_live_print)
        try:
            result = runner.run()
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            mgr.update_status(meta.id, "paused")
            return 1

    final_status = result.status
    turn_count = sum(1 for m in result.messages if m.role == "agent")
    mgr.update_status(meta.id, final_status, turns_completed=turn_count)

    print(f"\n{final_status.upper()}: {turn_count} agent messages in {transcript_path}")
    if result.pause_reason:
        print(result.pause_reason)
    for msg in result.messages:
        if msg.role != "system":
            print(f"  {msg.seq:03d} [{msg.author}] {msg.content[:120]}")

    return 0 if final_status == "completed" else 3


def _cmd_resume(argv: list[str]) -> int:
    from .config import load_config
    from .session import SessionManager

    parser = argparse.ArgumentParser(prog="relay resume", description="Resume a paused session.")
    parser.add_argument("session_id", nargs="?", help="Session ID (default: most recent paused).")
    parser.add_argument("--turns", type=int, help="Override max turns.")
    parser.add_argument("--no-limit", action="store_true", help="No turn limit.")
    parser.add_argument("--build", action="store_true", help="Enable build mode (if not already).")
    parser.add_argument("--tui", action="store_true", help="Launch split-pane terminal UI.")
    parser.add_argument("--web", action="store_true", help="Launch web viewer in browser.")
    parser.add_argument("--port", type=int, default=8411, help="Web viewer port (default: 8411).")
    args = parser.parse_args(argv)

    mgr = SessionManager()

    if args.session_id:
        session_id = args.session_id
    else:
        paused = mgr.list_sessions(status_filter="paused")
        if not paused:
            print("No paused sessions found.", file=sys.stderr)
            return 1
        session_id = paused[0].id
        print(f"Resuming most recent paused session: {session_id}")

    meta = mgr.get_session(session_id)
    transcript_path = mgr.get_transcript_path(session_id)

    if not transcript_path.exists():
        print(f"Transcript not found: {transcript_path}", file=sys.stderr)
        return 1

    # Load stored session from transcript to rebuild config
    stored_topic, stored_session = _load_stored_session(transcript_path)
    cfg = load_config()

    left_agent_data = stored_session.get("left_agent", {})
    right_agent_data = stored_session.get("right_agent", {})

    turns = args.turns or (999_999 if args.no_limit else cfg.turns)

    moderator_events = _moderator_events_from_snapshot(stored_session.get("moderator_events", []))

    config = RelayConfig(
        topic=stored_topic,
        turns=turns,
        moderator=stored_session.get("moderator", cfg.moderator),
        moderator_events=moderator_events,
        left_agent=AgentConfig(
            name=left_agent_data.get("name", cfg.left_name),
            provider=left_agent_data.get("provider", cfg.left_provider),
            model=left_agent_data.get("model", cfg.left_model),
            instruction=left_agent_data.get("instruction", ""),
        ),
        right_agent=AgentConfig(
            name=right_agent_data.get("name", cfg.right_name),
            provider=right_agent_data.get("provider", cfg.right_provider),
            model=right_agent_data.get("model", cfg.right_model),
            instruction=right_agent_data.get("instruction", ""),
        ),
    )

    # Handle --build upgrade on resume
    workspace_path = None
    if args.build:
        ws = mgr.get_workspace_path(session_id)
        if not ws.exists():
            from .workspace import WorkspaceManager
            wm = WorkspaceManager(ws)
            wm.setup(
                left_name=left_agent_data.get("name", "claude"),
                right_name=right_agent_data.get("name", "codex"),
            )
        workspace_path = ws
        meta = mgr.update_status(session_id, meta.status)  # keep status, just refresh

    starting_count = len(TranscriptStore(transcript_path).read())
    mgr.update_status(session_id, "running")

    if args.tui and args.web:
        print("ERROR: --tui and --web are mutually exclusive.", file=sys.stderr)
        return 1

    if args.tui:
        from .moderator import ModeratorInputQueue
        from .tui import run_relay_with_tui

        mq = ModeratorInputQueue()

        def runner_factory(moderator_queue=None, on_commit=None, on_stream_chunk=None, on_activity=None):
            return RelayRunner(
                config=config,
                out_path=transcript_path,
                moderator_queue=moderator_queue,
                on_commit=on_commit,
                on_stream_chunk=on_stream_chunk,
                on_activity=on_activity,
                workspace_path=workspace_path,
            )

        try:
            result = run_relay_with_tui(
                runner_factory=runner_factory,
                moderator_queue=mq,
                session_id=session_id,
                topic=stored_topic,
                resume=True,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if result is None:
            mgr.update_status(session_id, "paused")
            return 1
    elif args.web:
        from .moderator import ModeratorInputQueue
        from .web import run_relay_with_web

        mq = ModeratorInputQueue()

        def runner_factory(moderator_queue=None, on_commit=None, on_stream_chunk=None, on_activity=None):
            return RelayRunner(
                config=config,
                out_path=transcript_path,
                moderator_queue=moderator_queue,
                on_commit=on_commit,
                on_stream_chunk=on_stream_chunk,
                on_activity=on_activity,
                workspace_path=workspace_path,
            )

        try:
            result = run_relay_with_web(
                runner_factory=runner_factory,
                moderator_queue=mq,
                session_id=session_id,
                topic=stored_topic,
                resume=True,
                port=args.port,
                agents=[
                    {"name": config.left_agent.name, "provider": config.left_agent.provider, "model": config.left_agent.model},
                    {"name": config.right_agent.name, "provider": config.right_agent.provider, "model": config.right_agent.model},
                ],
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if result is None:
            mgr.update_status(session_id, "paused")
            return 1
    else:
        print(f"Session:  {session_id}")
        print(f"Topic:    {stored_topic}")
        print(f"Existing: {starting_count} messages")
        print()

        def _live_print(msg):
            if msg.role == "system":
                kind = msg.metadata.get("kind", "")
                if kind == "attempt_failed":
                    print(f"\n  [{msg.metadata.get('speaker', '?')} FAILED] {msg.content}", flush=True)
                elif kind == "pause":
                    print(f"\n  [PAUSED] {msg.content}", flush=True)
            elif msg.role != "system":
                print(f"\n  {msg.seq:03d} [{msg.author}]\n{msg.content}\n", flush=True)

        runner = RelayRunner(config=config, out_path=transcript_path, workspace_path=workspace_path, on_commit=_live_print)
        try:
            result = runner.run(resume=True)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    appended = result.messages[starting_count:]
    turn_count = sum(1 for m in result.messages if m.role == "agent")
    mgr.update_status(session_id, result.status, turns_completed=turn_count)

    print(f"\n{result.status.upper()}: appended {len(appended)} messages ({len(result.messages)} total)")
    if result.pause_reason:
        print(result.pause_reason)
    for msg in appended:
        if msg.role != "system":
            print(f"  {msg.seq:03d} [{msg.author}] {msg.content[:120]}")

    return 0 if result.status == "completed" else 3


def _cmd_list(argv: list[str]) -> int:
    from .session import SessionManager

    parser = argparse.ArgumentParser(prog="relay list", description="List relay sessions.")
    parser.add_argument("--all", action="store_true", help="Include completed and archived sessions.")
    parser.add_argument("--status", help="Filter by status.")
    args = parser.parse_args(argv)

    mgr = SessionManager()
    sessions = mgr.list_sessions(status_filter=args.status)

    if not args.all and not args.status:
        sessions = [s for s in sessions if s.status in ("new", "running", "paused")]

    if not sessions:
        print("No sessions found.")
        return 0

    print(f"{'ID':36s}  {'Status':10s}  {'Turns':5s}  {'Topic'}")
    print("-" * 90)
    for s in sessions:
        topic_short = s.topic[:40] + "..." if len(s.topic) > 40 else s.topic
        print(f"{s.id}  {s.status:10s}  {s.turns_completed:5d}  {topic_short}")

    return 0


def _cmd_archive(argv: list[str]) -> int:
    from .session import SessionManager

    parser = argparse.ArgumentParser(prog="relay archive", description="Archive a session.")
    parser.add_argument("session_id", help="Session ID to archive.")
    args = parser.parse_args(argv)

    mgr = SessionManager()
    try:
        mgr.archive_session(args.session_id)
        print(f"Archived: {args.session_id}")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_say(argv: list[str]) -> int:
    """Inject moderator input into a running session via its FIFO."""
    parser = argparse.ArgumentParser(prog="relay say", description="Inject moderator input.")
    parser.add_argument("message", nargs="+", help="Message to inject.")
    parser.add_argument("--session", help="Session ID (default: most recent running).")
    args = parser.parse_args(argv)

    from .session import SessionManager

    mgr = SessionManager()
    if args.session:
        session_id = args.session
    else:
        running = mgr.list_sessions(status_filter="running")
        if not running:
            print("No running sessions found.", file=sys.stderr)
            return 1
        session_id = running[0].id

    fifo_path = mgr.get_session_dir(session_id) / "input.fifo"
    if not fifo_path.exists():
        # Fall back to writing a plain file that the engine can check
        input_path = mgr.get_session_dir(session_id) / "human_input.md"
        input_path.write_text(" ".join(args.message) + "\n")
        print(f"Queued to {input_path}")
    else:
        with open(fifo_path, "w") as f:
            f.write(" ".join(args.message) + "\n")
        print(f"Sent to session {session_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(cli_entry(sys.argv[1:]))
