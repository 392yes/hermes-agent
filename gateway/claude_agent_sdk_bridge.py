"""Claude Agent SDK bridge for Clara lead turns.

This is the subscription-compatible fast path for Clara/Hermes when the user's
local Claude Code login is active.  It uses the official ``claude_agent_sdk``
package instead of calling Anthropic's Messages API directly, so it follows the
same Claude Code / ``claude -p`` subscription usage surface.

The module is intentionally optional: if the SDK package is unavailable or a
runtime/policy error occurs, callers should fall back to the existing Claude
Code CLI resident bridge.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterable


class ClaudeAgentSDKBridgeError(RuntimeError):
    """Raised when the SDK bridge cannot run a turn."""


def _split_tool_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _sdk_setting_sources(value: Any) -> list[str] | None:
    """Map config to Agent SDK setting_sources.

    ``none`` / ``[]`` gives a minimal fast path: no user/project/local Claude
    settings, which avoids user hooks/plugins and is much closer to the Codex
    provider latency profile.  ``None`` lets the SDK/Claude Code use defaults.
    """

    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value).strip().casefold()
    if text in {"", "none", "minimal", "off", "false", "0", "[]"}:
        return []
    if text in {"default", "all", "auto", "true", "1"}:
        return None
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _message_to_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "".join(parts).strip()


def _block_tool_name(block: Any) -> str | None:
    """Return a Claude SDK tool-use block name, if this block is a tool call."""
    name = getattr(block, "name", None)
    block_type = str(getattr(block, "type", "") or type(block).__name__).casefold()
    if name and ("tool" in block_type or hasattr(block, "input") or hasattr(block, "id")):
        return str(name)
    return None


def _block_tool_id(block: Any) -> str:
    return str(getattr(block, "id", None) or getattr(block, "tool_use_id", None) or "")


def _block_tool_input(block: Any) -> dict[str, Any]:
    value = getattr(block, "input", None)
    return value if isinstance(value, dict) else {}


def _tool_result_ids_from_message(message: Any) -> list[str]:
    """Extract Claude SDK tool result IDs from UserMessage variants."""
    ids: list[str] = []
    candidates: list[Any] = []
    direct = getattr(message, "tool_use_result", None)
    if direct is not None:
        candidates.append(direct)
    candidates.extend(getattr(message, "content", []) or [])
    for item in candidates:
        if isinstance(item, dict):
            value = item.get("tool_use_id") or item.get("id")
        else:
            value = getattr(item, "tool_use_id", None) or getattr(item, "id", None)
        if value:
            ids.append(str(value))
    return ids


def _hermes_tool_for_claude_tool(name: str) -> str:
    normalized = str(name or "").strip().casefold()
    return {
        "bash": "terminal",
        "read": "read_file",
        "write": "write_file",
        "edit": "patch",
        "multiedit": "patch",
        "notebookedit": "patch",
        "grep": "search_files",
        "glob": "search_files",
        "ls": "search_files",
        "websearch": "web_search",
        "webfetch": "web_extract",
    }.get(normalized, normalized or "claude_tool")


def _preview_for_claude_tool(name: str, args: dict[str, Any]) -> str:
    normalized = str(name or "").strip().casefold()
    if normalized == "bash":
        return str(args.get("command") or "Bash")
    if normalized == "read":
        return str(args.get("file_path") or args.get("path") or "Read")
    if normalized in {"grep", "glob"}:
        return str(args.get("pattern") or name)
    return str(name or "Claude tool")


def _jsonable_event(message: Any, *, elapsed: float) -> dict[str, Any]:
    event: dict[str, Any] = {
        "elapsed_seconds": round(elapsed, 3),
        "message_type": type(message).__name__,
    }
    for attr in ("subtype", "session_id", "uuid", "stop_reason", "model"):
        if hasattr(message, attr):
            value = getattr(message, attr)
            if value is not None:
                event[attr] = value
    if type(message).__name__ == "StreamEvent":
        event["event"] = getattr(message, "event", None)
    elif type(message).__name__ == "AssistantMessage":
        event["text"] = _message_to_text(message)
        event["usage"] = getattr(message, "usage", None)
    elif type(message).__name__ == "UserMessage":
        event["tool_use_result"] = getattr(message, "tool_use_result", None)
    elif type(message).__name__ == "RateLimitEvent":
        # Keep the structured repr in the job log; final user output never shows
        # this raw blob.  It is useful for confirming subscription path status.
        event["rate_limit"] = repr(message)
    elif type(message).__name__ == "ResultMessage":
        for attr in (
            "duration_ms",
            "duration_api_ms",
            "is_error",
            "num_turns",
            "total_cost_usd",
            "usage",
        ):
            if hasattr(message, attr):
                event[attr] = getattr(message, attr)
    return event


def _get_event_value(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


def _shorten(value: Any, limit: int = 80) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: max(0, limit - 3)] + "..."
    return text


def _sdk_progress_text(message: Any) -> tuple[str, str] | None:
    """Return ``(event_type, text)`` for user-facing SDK progress.

    Claude Agent SDK surfaces Claude Code stream-json events as structured
    ``StreamEvent`` objects.  The raw shape varies by SDK/CLI version, so this
    stays duck-typed and compact: enough to prove Clara is actively
    thinking/writing/using tools, without exposing final content or raw tool
    result payloads in the progress line.
    """

    message_type = type(message).__name__
    if message_type == "StreamEvent":
        event = getattr(message, "event", None)
        event_type = str(_get_event_value(event, "type") or "stream_event")
        block_type = str(_get_event_value(event, "content_block", "type") or "")
        delta_type = str(_get_event_value(event, "delta", "type") or "")
        tool_name = _get_event_value(event, "content_block", "name") or _get_event_value(event, "name")

        if event_type == "content_block_start" and block_type == "tool_use":
            return "sdk.tool.started", f"Claude tool 시작: {_shorten(tool_name or 'tool')}"
        if event_type in {"tool_use", "tool_call"}:
            return "sdk.tool.started", f"Claude tool 시작: {_shorten(tool_name or 'tool')}"
        if event_type in {"content_block_stop", "tool_result"} and (block_type == "tool_use" or tool_name):
            return "sdk.tool.completed", f"Claude tool 완료: {_shorten(tool_name or 'tool')}"
        if delta_type == "text_delta":
            return None
        if event_type in {"message_start", "message_delta"}:
            return None
        if event_type in {"message_stop", "done"}:
            return None
        # Do not surface raw Claude Code stream-json event names such as
        # content_block_start/stop/delta.  They polluted the fixed pane status
        # bar with implementation details instead of useful progress text.
        return None

    if message_type == "AssistantMessage":
        return None
    if message_type == "UserMessage":
        return None
    if message_type == "RateLimitEvent":
        return None
    if message_type == "ResultMessage":
        return None
    return None


def _emit_progress(
    progress_callback: Callable[..., Any] | None,
    event_type: str,
    text: str,
    data: dict[str, Any] | None = None,
) -> None:
    if not progress_callback or not text:
        return
    try:
        progress_callback(event_type, text, data or {})
    except Exception:
        # Presentation-only; never fail the Claude turn because a TUI repaint or
        # Slack edit failed.
        pass


async def _run_sdk_turn_async(
    *,
    prompt: str,
    workdir: str,
    claude_bin: str,
    max_turns: int,
    timeout: int,
    permission_mode: str,
    model: str,
    effort: str,
    allowed_tools: list[str],
    tools_mode: str,
    setting_sources: list[str] | None,
    include_partial_messages: bool,
    include_hook_events: bool,
    resume_session_id: str | None,
    log_dir: Path,
    progress_callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise ClaudeAgentSDKBridgeError(
            "claude-agent-sdk package is not installed in the active Hermes venv"
        ) from exc

    tools: Any
    normalized_tools = str(tools_mode or "claude_code").strip().casefold()
    if normalized_tools in {"none", "off", "false", "0", "[]"}:
        tools = []
    elif normalized_tools in {"", "claude_code", "default", "preset"}:
        tools = {"type": "preset", "preset": "claude_code"}
    else:
        tools = _split_tool_csv(tools_mode)

    opts_kwargs: dict[str, Any] = {
        "cwd": workdir,
        "cli_path": claude_bin,
        "permission_mode": permission_mode or "bypassPermissions",
        "max_turns": max_turns,
        "include_partial_messages": include_partial_messages,
        "include_hook_events": include_hook_events,
        "mcp_servers": {},
        "skills": [],
        "plugins": [],
        "tools": tools,
        "setting_sources": setting_sources,
    }
    if allowed_tools:
        opts_kwargs["allowed_tools"] = allowed_tools
    if model:
        opts_kwargs["model"] = model
    if effort:
        opts_kwargs["effort"] = effort
    if resume_session_id:
        opts_kwargs["resume"] = resume_session_id

    options = ClaudeAgentOptions(**opts_kwargs)

    started = time.time()
    events: list[dict[str, Any]] = []
    latest_text = ""
    result_event: dict[str, Any] | None = None
    session_id: str | None = None
    latest_model: str | None = None
    latest_usage: Any = None
    counts: dict[str, int] = {}
    last_progress_text = ""
    last_progress_at = 0.0
    streamed_text = ""
    active_tools: dict[str, tuple[str, dict[str, Any], str]] = {}

    async def _consume() -> None:
        nonlocal latest_text, result_event, session_id, latest_model, latest_usage
        nonlocal last_progress_text, last_progress_at, streamed_text
        async for message in query(prompt=prompt, options=options):
            elapsed = time.time() - started
            typ = type(message).__name__
            counts[typ] = counts.get(typ, 0) + 1
            event = _jsonable_event(message, elapsed=elapsed)
            events.append(event)
            progress = _sdk_progress_text(message)
            if progress:
                progress_event_type, progress_text = progress
                now = time.monotonic()
                force = progress_event_type not in {"sdk.text_delta", "sdk.thinking"}
                if force or progress_text != last_progress_text or (now - last_progress_at) >= 2.0:
                    last_progress_text = progress_text
                    last_progress_at = now
                    _emit_progress(progress_callback, progress_event_type, progress_text, event)
            sid = getattr(message, "session_id", None)
            if sid:
                session_id = str(sid)
            if typ == "AssistantMessage":
                latest_model = str(getattr(message, "model", "") or "") or latest_model
                latest_usage = getattr(message, "usage", None) or latest_usage
                text = _message_to_text(message)
                if text:
                    latest_text = text
                    delta = text[len(streamed_text):] if text.startswith(streamed_text) else text
                    if delta:
                        _emit_progress(progress_callback, "clara.assistant.delta", delta, event)
                        streamed_text = text
                for block in getattr(message, "content", []) or []:
                    tool_name = _block_tool_name(block)
                    if not tool_name:
                        continue
                    _emit_progress(progress_callback, "clara.assistant.boundary", "boundary", event)
                    tool_id = _block_tool_id(block)
                    tool_args = _block_tool_input(block)
                    hermes_tool = _hermes_tool_for_claude_tool(tool_name)
                    preview = _preview_for_claude_tool(tool_name, tool_args)
                    if tool_id:
                        active_tools[tool_id] = (hermes_tool, tool_args, preview)
                    _emit_progress(
                        progress_callback,
                        "clara.tool.started",
                        preview,
                        {**event, "tool_name": hermes_tool, "claude_tool_name": tool_name, "tool_id": tool_id, "tool_args": tool_args, "preview": preview},
                    )
            elif typ == "UserMessage":
                result = getattr(message, "tool_use_result", None)
                for tool_id in _tool_result_ids_from_message(message):
                    if tool_id and tool_id in active_tools:
                        hermes_tool, tool_args, preview = active_tools.pop(tool_id)
                        _emit_progress(
                            progress_callback,
                            "clara.tool.completed",
                            preview,
                            {**event, "tool_name": hermes_tool, "tool_id": tool_id, "tool_args": tool_args, "preview": preview, "result": result},
                        )
            elif typ == "ResultMessage":
                result_event = event

    await asyncio.wait_for(_consume(), timeout=max(1, timeout))
    (log_dir / "sdk-events.jsonl").write_text(
        "".join(json.dumps(ev, ensure_ascii=False, default=str) + "\n" for ev in events),
        encoding="utf-8",
    )
    if result_event is None:
        raise ClaudeAgentSDKBridgeError("Claude Agent SDK completed without ResultMessage")
    is_error = bool(result_event.get("is_error")) or str(result_event.get("subtype") or "success") != "success"
    return {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "result": latest_text,
        "session_id": session_id or result_event.get("session_id"),
        "duration_ms": result_event.get("duration_ms"),
        "duration_api_ms": result_event.get("duration_api_ms"),
        "num_turns": result_event.get("num_turns"),
        "total_cost_usd": result_event.get("total_cost_usd"),
        "usage": result_event.get("usage"),
        "model": latest_model,
        "modelUsage": {latest_model: latest_usage} if latest_model else {},
        "event_counts": counts,
        "delivery": "agent-sdk",
    }


def run_sdk_turn(
    *,
    prompt: str,
    workdir: str,
    claude_bin: str,
    max_turns: int,
    timeout: int,
    permission_mode: str,
    model: str = "",
    effort: str = "",
    allowed_tools: Iterable[str] | None = None,
    tools_mode: str = "claude_code",
    setting_sources: Any = None,
    include_partial_messages: bool = True,
    include_hook_events: bool = False,
    resume_session_id: str | None = None,
    log_dir: Path,
    progress_callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run one Claude Agent SDK turn and return a Claude-CLI-shaped result dict."""

    try:
        return asyncio.run(
            _run_sdk_turn_async(
                prompt=prompt,
                workdir=workdir,
                claude_bin=claude_bin,
                max_turns=max_turns,
                timeout=timeout,
                permission_mode=permission_mode,
                model=model,
                effort=effort,
                allowed_tools=list(allowed_tools or []),
                tools_mode=tools_mode,
                setting_sources=_sdk_setting_sources(setting_sources),
                include_partial_messages=include_partial_messages,
                include_hook_events=include_hook_events,
                resume_session_id=resume_session_id,
                log_dir=log_dir,
                progress_callback=progress_callback,
            )
        )
    except asyncio.TimeoutError as exc:
        raise ClaudeAgentSDKBridgeError(f"Claude Agent SDK timed out after {timeout}s") from exc
