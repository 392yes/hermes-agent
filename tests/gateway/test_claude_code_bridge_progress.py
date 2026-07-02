import sys
import types
from pathlib import Path

import pytest

from gateway.claude_agent_sdk_bridge import _enforce_sdk_default_max_buffer_size
from gateway.claude_agent_sdk_bridge import run_sdk_turn
from gateway.claude_code_bridge import run_claude_code_bridge_resident


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class AssistantMessage:
    def __init__(self, content, model="claude-test"):
        self.content = content
        self.model = model


class StreamEvent:
    def __init__(self, event):
        self.event = event


class UserMessage:
    def __init__(self, tool_use_result):
        self.tool_use_result = tool_use_result


class ResultMessage:
    subtype = "success"
    is_error = False
    duration_ms = 1
    duration_api_ms = 1
    num_turns = 1
    total_cost_usd = 0
    usage = {}


class ErrorResultMessage(ResultMessage):
    is_error = True
    api_error_status = 401


def test_sdk_turn_emits_clara_streaming_and_structured_tool_events(monkeypatch, tmp_path):
    captured_options = []

    async def fake_query(prompt, options):
        captured_options.append(options)
        yield AssistantMessage([TextBlock("변경 범위 확인\n"), ToolUseBlock("tool-1", "Bash", {"command": "git status --short"})])
        yield UserMessage({"tool_use_id": "tool-1", "content": " M cli.py"})
        yield AssistantMessage([TextBlock("변경 범위 확인\n결과 정리\n")])
        yield ResultMessage()

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_sdk = types.SimpleNamespace(ClaudeAgentOptions=ClaudeAgentOptions, query=fake_query)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    events = []
    result = run_sdk_turn(
        prompt="prompt",
        workdir=str(tmp_path),
        claude_bin="claude",
        max_turns=3,
        timeout=10,
        permission_mode="bypassPermissions",
        allowed_tools=["Bash"],
        tools_mode="claude_code",
        setting_sources="none",
        log_dir=tmp_path,
        progress_callback=lambda event_type, text, data=None: events.append((event_type, text, data or {})),
    )

    assert result["delivery"] == "agent-sdk"
    assert result["result"] == "변경 범위 확인\n결과 정리"
    assert captured_options[0].kwargs["max_buffer_size"] == 8 * 1024 * 1024
    assert captured_options[0].kwargs["strict_mcp_config"] is False
    assert ("clara.assistant.delta", "변경 범위 확인") in [(event[0], event[1]) for event in events]
    event_types = [event[0] for event in events]
    assert "clara.assistant.boundary" in event_types
    assert "clara.tool.started" in event_types
    assert "clara.tool.completed" in event_types
    started = next(event for event in events if event[0] == "clara.tool.started")
    assert started[2]["tool_name"] == "terminal"
    assert started[2]["tool_args"]["command"] == "git status --short"
    deltas = [event[1] for event in events if event[0] == "clara.assistant.delta"]
    assert deltas == ["변경 범위 확인", "\n결과 정리"]


def test_sdk_turn_streams_raw_text_delta_without_duplicate_snapshot(monkeypatch, tmp_path):
    async def fake_query(prompt, options):
        yield StreamEvent({"type": "message_start"})
        yield StreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "진행 "}})
        yield StreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "중"}})
        yield AssistantMessage([TextBlock("진행 중")])
        yield StreamEvent({"type": "message_start"})
        yield StreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "완료"}})
        yield AssistantMessage([TextBlock("완료")])
        yield ResultMessage()

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_sdk = types.SimpleNamespace(ClaudeAgentOptions=ClaudeAgentOptions, query=fake_query)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    events = []
    result = run_sdk_turn(
        prompt="prompt",
        workdir=str(tmp_path),
        claude_bin="claude",
        max_turns=3,
        timeout=10,
        permission_mode="bypassPermissions",
        allowed_tools=[],
        tools_mode="claude_code",
        setting_sources="none",
        log_dir=tmp_path,
        progress_callback=lambda event_type, text, data=None: events.append((event_type, text, data or {})),
    )

    assert result["delivery"] == "agent-sdk"
    assert result["result"] == "완료"
    deltas = [event[1] for event in events if event[0] == "clara.assistant.delta"]
    assert deltas == ["진행 ", "중", "완료"]


def test_sdk_turn_preserves_partial_event_log_when_sdk_raises(monkeypatch, tmp_path):
    async def fake_query(prompt, options):
        yield AssistantMessage([TextBlock("파일 쓰는 중")])
        raise Exception("Claude Code returned an error result: Reached maximum number of turns (8)")

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_sdk = types.SimpleNamespace(ClaudeAgentOptions=ClaudeAgentOptions, query=fake_query)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    with pytest.raises(Exception, match="Reached maximum number of turns"):
        run_sdk_turn(
            prompt="prompt",
            workdir=str(tmp_path),
            claude_bin="claude",
            max_turns=8,
            timeout=10,
            permission_mode="bypassPermissions",
            allowed_tools=[],
            tools_mode="claude_code",
            setting_sources="none",
            log_dir=tmp_path,
        )

    event_log = tmp_path / "sdk-events.jsonl"
    assert event_log.exists()
    assert "파일 쓰는 중" in event_log.read_text(encoding="utf-8")


def test_sdk_turn_renders_error_result_even_when_sdk_raises_after_result(monkeypatch, tmp_path):
    async def fake_query(prompt, options):
        yield AssistantMessage([TextBlock("Failed to authenticate. API Error: 401 Invalid authentication credentials")])
        yield ErrorResultMessage()
        raise Exception("Claude Code returned an error result: success")

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_sdk = types.SimpleNamespace(ClaudeAgentOptions=ClaudeAgentOptions, query=fake_query)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    result = run_sdk_turn(
        prompt="prompt",
        workdir=str(tmp_path),
        claude_bin="claude",
        max_turns=8,
        timeout=10,
        permission_mode="bypassPermissions",
        allowed_tools=[],
        tools_mode="claude_code",
        setting_sources="none",
        log_dir=tmp_path,
    )

    assert result["is_error"] is True
    assert result["subtype"] == "error"
    assert result["api_error_status"] == 401
    assert "401 Invalid authentication credentials" in result["result"]
    assert "Claude Code returned an error result: success" in result["sdk_exception"]
    assert (tmp_path / "sdk-events.jsonl").exists()


def test_sdk_max_turn_exception_renders_as_continuation_not_generic_failure(monkeypatch, tmp_path):
    def boom(**_kwargs):
        raise Exception("Claude Code returned an error result: Reached maximum number of turns (8)")

    monkeypatch.setattr("gateway.claude_agent_sdk_bridge.run_sdk_turn", boom)
    result = run_claude_code_bridge_resident(
        config={"clara_cli": {"sdk_enabled": True, "resident_enabled": True, "command": "claude", "sdk_max_turns": 8}},
        message="hi",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
        bridge_session_key="cli:test",
    )

    assert result.exit_code == 1
    assert "작업 제한(max_turns)에 도달" in result.final_response
    assert "현재 제한: max_turns=8" in result.final_response
    assert "SDK 경로가 실패" not in result.final_response


def test_sdk_bridge_passes_buffer_and_strict_mcp_config(monkeypatch, tmp_path):
    captured = {}

    def fake_run_sdk_turn(**kwargs):
        captured.update(kwargs)
        return {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}

    monkeypatch.setattr("gateway.claude_agent_sdk_bridge.run_sdk_turn", fake_run_sdk_turn)
    result = run_claude_code_bridge_resident(
        config={
            "clara_cli": {
                "sdk_enabled": True,
                "resident_enabled": True,
                "command": "claude",
                "sdk_max_buffer_size": 16777216,
                "strict_mcp": True,
            }
        },
        message="hi",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
        bridge_session_key="cli:test",
    )

    assert result.exit_code == 0
    assert captured["max_buffer_size"] == 16777216
    assert captured["strict_mcp_config"] is True


def test_sdk_bridge_records_buffer_limit_in_metadata(monkeypatch, tmp_path):
    def fake_run_sdk_turn(**kwargs):
        return {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}

    monkeypatch.setattr("gateway.claude_agent_sdk_bridge.run_sdk_turn", fake_run_sdk_turn)
    result = run_claude_code_bridge_resident(
        config={
            "clara_cli": {
                "sdk_enabled": True,
                "resident_enabled": True,
                "command": "claude",
                "sdk_max_buffer_size": 16777216,
            }
        },
        message="hi",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
        bridge_session_key="cli:test",
    )

    metadata = (Path(result.log_dir) / "metadata.json").read_text(encoding="utf-8")
    assert '\"sdk_max_buffer_size\": 16777216' in metadata


def test_sdk_bridge_raises_transport_default_buffer_limit(monkeypatch):
    transport = types.SimpleNamespace(_DEFAULT_MAX_BUFFER_SIZE=1024 * 1024)

    def fake_import_module(name):
        assert name == "claude_agent_sdk._internal.transport.subprocess_cli"
        return transport

    monkeypatch.setattr("gateway.claude_agent_sdk_bridge.importlib.import_module", fake_import_module)
    _enforce_sdk_default_max_buffer_size(16 * 1024 * 1024)

    assert transport._DEFAULT_MAX_BUFFER_SIZE == 16 * 1024 * 1024


def test_sdk_enabled_failure_does_not_fallback_to_raw_claude_cli(monkeypatch, tmp_path):
    def boom(**_kwargs):
        raise RuntimeError("sdk unavailable")

    monkeypatch.setattr("gateway.claude_agent_sdk_bridge.run_sdk_turn", boom)
    progress = []
    result = run_claude_code_bridge_resident(
        config={"clara_cli": {"sdk_enabled": True, "resident_enabled": True, "command": "claude"}},
        message="hi",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
        bridge_session_key="cli:test",
        progress_callback=lambda event_type, text, data=None: progress.append(event_type),
    )

    assert result.exit_code == 1
    assert "Claude Agent SDK 경로가 실패" in result.final_response
    assert "CLI fallback은 실행하지 않았습니다" in "\n".join(progress) or "sdk.error" in progress
    assert (Path(result.log_dir) / "sdk-error.log").exists()
    assert not (Path(result.log_dir) / "sdk-fallback.log").exists()
