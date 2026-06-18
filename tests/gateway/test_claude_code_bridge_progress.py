import sys
import types
from pathlib import Path

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


def test_sdk_turn_emits_clara_streaming_and_structured_tool_events(monkeypatch, tmp_path):
    async def fake_query(prompt, options):
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
