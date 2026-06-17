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
            return "sdk.text_delta", "Claude 응답 작성 중"
        if event_type in {"message_start", "message_delta"}:
            return "sdk.thinking", "Claude 응답 생성 중"
        if event_type in {"message_stop", "done"}:
            return "sdk.message_stop", "Claude 응답 정리 중"
        if event_type and event_type != "stream_event":
            return "sdk.stream_event", f"Claude stream_event: {_shorten(event_type, 40)}"
        return "sdk.stream_event", "Claude stream_event 수신 중"

    if message_type == "AssistantMessage":
        return "sdk.assistant", "Claude 응답 정리 중"
    if message_type == "UserMessage":
        return "sdk.tool.result", "Claude tool 결과 처리 중"
    if message_type == "RateLimitEvent":
        return "sdk.rate_limit", "Claude 사용량/레이트 상태 확인 중"
    if message_type == "ResultMessage":
        return "sdk.result", "Claude 작업 완료"
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


def _emit_tool_event(
    progress_callback: Callable[..., Any] | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Emit a structured tool-lifecycle event (no human text).

    Unlike :func:`_emit_progress` this is *not* gated on a text string: it
    carries normalized ``data`` (hermes_tool / tool_args / duration) that a
    rich consumer (e.g. the CLI/TUI codex-style scrollback) renders directly.
    Text-only consumers (Slack heartbeat) receive an empty ``text`` and ignore
    it, so this never duplicates the existing ``sdk.*`` heartbeat lines.
    """
    if not progress_callback:
        return
    try:
        progress_callback(event_type, "", data or {})
    except Exception:
        pass


def _is_tool_use_block(block: Any) -> bool:
    if type(block).__name__ == "ToolUseBlock":
        return True
    return (
        getattr(block, "name", None) is not None
        and hasattr(block, "input")
        and getattr(block, "id", None) is not None
    )


def _map_sdk_tool(name: str, inp: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Map a Claude Code SDK tool (Bash/Read/Edit/...) to Hermes display tool.

    Returns ``(hermes_tool_name, display_args, preview)`` so the existing
    ``agent.display.get_cute_tool_message`` renderer produces the same
    ``💻 $ / 📖 read / 🔧 patch / 🔎 grep`` lines codex-lead already shows.
    """
    inp = inp if isinstance(inp, dict) else {}
    n = str(name or "").strip().casefold()
    file_path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
    if n == "bash":
        cmd = str(inp.get("command", ""))
        return "terminal", {"command": cmd}, _shorten(cmd, 60)
    if n == "read":
        return "read_file", {"path": file_path}, str(file_path)
    if n == "write":
        return "write_file", {"path": file_path}, str(file_path)
    if n in {"edit", "multiedit", "notebookedit"}:
        return "patch", {"path": file_path}, str(file_path)
    if n == "grep":
        pat = str(inp.get("pattern", ""))
        return "search_files", {"pattern": pat, "target": "content"}, pat
    if n == "glob":
        pat = str(inp.get("pattern", ""))
        return "search_files", {"pattern": pat, "target": "files"}, pat
    if n == "websearch":
        q = str(inp.get("query", ""))
        return "web_search", {"query": q}, q
    if n == "webfetch":
        url = str(inp.get("url", ""))
        return "web_extract", {"urls": [url] if url else []}, url
    if n == "todowrite":
        return "todo", {"todos": inp.get("todos"), "merge": inp.get("merge", False)}, "tasks"
    if n == "task":
        goal = str(inp.get("description") or inp.get("prompt") or "")
        return "delegate_task", {"goal": goal}, goal
    safe_args = {k: v for k, v in inp.items() if isinstance(v, (str, int, float, bool))}
    return (n or "tool"), safe_args, ""


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
    counts: dict[str, int] = {}
    last_progress_text = ""
    last_progress_at = 0.0
    # tool_id -> (hermes_tool, start_elapsed); pairs ToolUseBlock (started, in
    # AssistantMessage) with its tool_result (completed, in the next UserMessage)
    # so the CLI/TUI can render codex-style per-tool scrollback lines.
    pending_tools: dict[str, tuple[str, float]] = {}

    async def _consume() -> None:
        nonlocal latest_text, result_event, session_id
        nonlocal last_progress_text, last_progress_at
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
                text = _message_to_text(message)
                if text:
                    latest_text = text
                # Structured tool-start: ToolUseBlocks carry the complete input,
                # so emit a codex-style "started" with normalized name + args.
                for block in getattr(message, "content", []) or []:
                    if not _is_tool_use_block(block):
                        continue
                    tool_id = str(getattr(block, "id", "") or "")
                    sdk_tool = str(getattr(block, "name", "") or "")
                    tool_input = getattr(block, "input", {}) or {}
                    hermes_tool, tool_args, preview = _map_sdk_tool(sdk_tool, tool_input)
                    if tool_id:
                        pending_tools[tool_id] = (hermes_tool, elapsed)
                    _emit_tool_event(
                        progress_callback,
                        "clara.tool.started",
                        {
                            "hermes_tool": hermes_tool,
                            "tool_args": tool_args,
                            "preview": preview,
                            "sdk_tool": sdk_tool,
                        },
                    )
            elif typ == "UserMessage":
                # tool_result blocks close out the matching started events.
                for block in getattr(message, "content", []) or []:
                    tool_id = str(getattr(block, "tool_use_id", "") or "")
                    if not tool_id or tool_id not in pending_tools:
                        continue
                    hermes_tool, start_elapsed = pending_tools.pop(tool_id)
                    _emit_tool_event(
                        progress_callback,
                        "clara.tool.completed",
                        {
                            "hermes_tool": hermes_tool,
                            "duration": max(0.0, elapsed - start_elapsed),
                            "is_error": bool(getattr(block, "is_error", False)),
                        },
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
