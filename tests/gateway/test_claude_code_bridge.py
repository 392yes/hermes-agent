import json
import os
import sqlite3
from pathlib import Path

from gateway.claude_code_bridge import (
    build_claude_prompt,
    build_continuity_context,
    extract_explicit_workdir,
    is_claude_code_cli_config,
    resolve_workdir,
    run_claude_agent_sdk_bridge,
    run_claude_code_bridge_sync,
)


def test_detects_claude_code_cli_provider():
    assert is_claude_code_cli_config({"model": {"provider": "claude-code-cli"}})
    assert is_claude_code_cli_config({"clara_cli": {"enabled": True}})
    assert not is_claude_code_cli_config({"model": {"provider": "anthropic"}})


def test_extract_explicit_workdir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert extract_explicit_workdir(f"클라라 리뷰해줘 {project}, diff 확인") == str(project)


def test_resolve_workdir_prefers_prompt_path(tmp_path):
    default = tmp_path / "default"
    explicit = tmp_path / "explicit"
    default.mkdir()
    explicit.mkdir()
    cfg = {"clara_cli": {"workdir": str(default)}}
    assert resolve_workdir(cfg, f"review {explicit}") == str(explicit)


def test_build_prompt_includes_write_authority_boundary():
    prompt = build_claude_prompt(
        message="테스트 실패 로그 분석해줘",
        context_prompt="ctx",
        channel_prompt="channel",
        history=[{"role": "user", "content": "이전 요청"}],
        workdir="/tmp/project",
        continuity_context="continuity packet",
        role_mode="clara-lead",
    )
    assert "Clara/클라라" in prompt
    assert "Clara is the coding orchestrator, not a review-only role." in prompt
    assert "answer directly and avoid broad repository/tool exploration unless needed" in prompt
    assert "inspect, edit, run, verify, and report end-to-end" in prompt
    assert "same operational authority" in prompt
    assert "inspect, edit, run commands" in prompt
    assert "continuity packet" in prompt
    assert "테스트 실패 로그 분석해줘" in prompt
    assert "/tmp/project" in prompt
    assert "Return a Slack-ready Clara response" in prompt


def test_build_prompt_without_channel_uses_hermes_cli_response_format():
    prompt = build_claude_prompt(
        message="보기 쉽게 요약해줘",
        context_prompt="ctx",
        channel_prompt=None,
        history=[],
        workdir="/tmp/project",
        role_mode="clara-lead",
    )
    assert "Return a Hermes CLI-ready Clara response" in prompt
    assert "native Hermes/Codex runtime" in prompt
    assert "one short topic/title line" in prompt
    assert "compact core summary" in prompt
    assert "grouped bullet lists" in prompt
    assert "verification results and remaining actions" in prompt
    assert "Do not include a Slack role marker" in prompt
    assert "Return a Slack-ready Clara response" not in prompt


def test_build_prompt_with_channel_keeps_slack_response_format():
    prompt = build_claude_prompt(
        message="보고해줘",
        context_prompt="ctx",
        channel_prompt="channel",
        history=[],
        workdir="/tmp/project",
        role_mode="clara-lead",
    )
    assert "Return a Slack-ready Clara response" in prompt
    assert "🟪 Clara/클라라 —" in prompt
    assert "Return a Hermes CLI-ready Clara response" not in prompt


def test_build_prompt_in_clara_lead_overrides_hugo_channel_marker():
    prompt = build_claude_prompt(
        message="보고해줘",
        context_prompt="ctx",
        channel_prompt="Always start every Slack reply with '🟦 Hugo/휴고 — '",
        history=[],
        workdir="/tmp/project",
        role_mode="clara-lead",
    )
    assert "🟪 Clara/클라라 —" in prompt
    assert "Do not use the Hugo/휴고 marker" in prompt
    assert "🟦 Hugo/휴고 —" not in prompt


def test_build_continuity_context_reads_session_snippets(tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    con = sqlite3.connect(hermes_home / "state.db")
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='id');
        INSERT INTO sessions (id, title) VALUES ('s1', 'WPC order benchmark');
        INSERT INTO messages (id, session_id, role, content, timestamp)
        VALUES (1, 's1', 'assistant', 'WorkPilot_Commerce previous implementation decision', 100.0);
        INSERT INTO messages_fts(rowid, content) VALUES (1, 'WorkPilot_Commerce previous implementation decision');
        """
    )
    con.close()

    context = build_continuity_context(
        hermes_home=hermes_home,
        message="WorkPilot_Commerce 이어서 해줘",
        workdir="/Users/392yes/project/001_WorkPilot_Commerce",
    )
    assert "Mode-independent continuity context" in context
    assert "hugo-lead and clara-lead" in context
    assert "WorkPilot_Commerce" in context
    assert "WPC order benchmark" in context


def test_build_continuity_context_collapses_profile_home_to_canonical_root(tmp_path):
    canonical = tmp_path / ".hermes"
    profile_home = canonical / "profiles" / "wpcorderbot"
    profile_home.mkdir(parents=True)

    context = build_continuity_context(
        hermes_home=profile_home,
        message="WPC 이어서",
        workdir=None,
    )
    assert f"Canonical Hermes home/session DB: {canonical}" in context


def test_build_continuity_context_surfaces_session_handover_file(tmp_path):
    hermes_home = tmp_path / "hermes"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    handover = repo / "handover.md"
    handover.write_text(
        "---\ntype: session-handover\ncanonical: true\n---\n# Session Handover\n",
        encoding="utf-8",
    )

    context = build_continuity_context(
        hermes_home=hermes_home,
        message="/session-resume 이어서",
        workdir=str(repo),
    )

    assert "Session handoff continuity" in context
    assert "/session-handoff writes canonical handover.md plus an Obsidian copy" in context
    assert "HERMES_CLARA_DISABLE_RESUME only disables Claude Code native session resume" in context
    assert str(handover) in context


def test_run_bridge_removes_anthropic_api_key_and_parses_noisy_json(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        "assert '--permission-mode' in sys.argv\n"
        "print('warn: noisy prefix')\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'OK'}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    result = run_claude_code_bridge_sync(
        config={
            "clara_cli": {
                "command": str(fake),
                "workdir": str(workdir),
                "allowed_tools": "Read",
                "max_turns": 1,
                "timeout_seconds": 10,
            }
        },
        message="ping",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path / "hermes",
    )
    assert result.exit_code == 0
    assert result.final_response.startswith("🟪 Clara/클라라 — OK")
    assert Path(result.log_dir, "result.json").exists()
    assert json.loads(Path(result.log_dir, "result.json").read_text())["result"] == "OK"


def test_run_bridge_strips_role_marker_for_cli_output(tmp_path):
    # In CLI/Wave the Hermes panel title is the speaker label, so any model-emitted
    # Slack marker should be stripped from the response body.
    model_output = "🟪 Clara/클라라 —\n\n**결론: 준비 완료**"
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({{'type':'result','subtype':'success','is_error':False,'result':{model_output!r}}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = run_claude_code_bridge_sync(
        config={
            "clara_cli": {
                "command": str(fake),
                "workdir": str(workdir),
                "allowed_tools": "Read",
                "max_turns": 1,
                "timeout_seconds": 10,
            }
        },
        message="ping",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path / "hermes",
        bridge_session_key="cli:test-pane",
    )
    assert result.exit_code == 0
    assert "🟪 Clara/클라라 —" not in result.final_response
    assert result.final_response.startswith("**결론: 준비 완료**")


def test_run_bridge_keeps_single_role_marker_for_slack_output(tmp_path):
    model_output = "🟪 Clara/클라라 —\n\n**결론: 준비 완료**"
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({{'type':'result','subtype':'success','is_error':False,'result':{model_output!r}}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    result = run_claude_code_bridge_sync(
        config={
            "clara_cli": {
                "command": str(fake),
                "workdir": str(workdir),
                "allowed_tools": "Read",
                "max_turns": 1,
                "timeout_seconds": 10,
            }
        },
        message="ping",
        context_prompt=None,
        channel_prompt="Slack channel instruction",
        history=[],
        hermes_home=tmp_path / "hermes",
    )
    assert result.exit_code == 0
    assert result.final_response.count("🟪 Clara/클라라 —") == 1
    assert result.final_response.startswith("🟪 Clara/클라라 — **결론: 준비 완료**")


def test_resolve_workdir_prefers_config_when_no_explicit_path(tmp_path):
    configured_project = tmp_path / "configured-project"
    configured_project.mkdir()

    workdir = resolve_workdir(
        {"claude_code_cli": {"workdir": str(configured_project)}},
        "이 프로젝트에서 계속 작업해줘",
        hermes_home=tmp_path / ".hermes",
    )

    assert workdir == str(configured_project)


def test_resolve_workdir_keeps_explicit_prompt_path_above_config(tmp_path):
    configured_project = tmp_path / "configured-project"
    explicit_project = tmp_path / "explicit-project"
    configured_project.mkdir()
    explicit_project.mkdir()

    workdir = resolve_workdir(
        {"claude_code_cli": {"workdir": str(configured_project)}},
        f"{explicit_project} 여기에서 작업해줘",
        hermes_home=tmp_path / ".hermes",
    )

    assert workdir == str(explicit_project)


def _make_fake_claude_asserting_max_turns(path, expected):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "idx = sys.argv.index('--max-turns')\n"
        f"assert sys.argv[idx + 1] == {expected!r}, sys.argv[idx + 1]\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'OK'}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_run_bridge_prefers_clara_cli_max_turns_for_claude_cli(tmp_path):
    # clara_cli.max_turns is the bridge-specific speed-tuning knob and must take
    # precedence over the generic agent.max_turns when both are set.
    fake = tmp_path / "claude"
    _make_fake_claude_asserting_max_turns(fake, "99")
    workdir = tmp_path / "repo"
    workdir.mkdir()

    result = run_claude_code_bridge_sync(
        config={
            "agent": {"max_turns": 7},
            "clara_cli": {
                "command": str(fake),
                "workdir": str(workdir),
                "allowed_tools": "Read",
                "max_turns": 99,
                "timeout_seconds": 10,
            },
        },
        message="ping",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path / "hermes",
    )

    assert result.exit_code == 0
    assert result.final_response.startswith("🟪 Clara/클라라 — OK")


def test_run_bridge_falls_back_to_agent_max_turns_when_clara_cli_unset(tmp_path):
    # When clara_cli.max_turns is not configured, the generic agent.max_turns
    # is still honored as the fallback.
    fake = tmp_path / "claude"
    _make_fake_claude_asserting_max_turns(fake, "7")
    workdir = tmp_path / "repo"
    workdir.mkdir()

    result = run_claude_code_bridge_sync(
        config={
            "agent": {"max_turns": 7},
            "clara_cli": {
                "command": str(fake),
                "workdir": str(workdir),
                "allowed_tools": "Read",
                "timeout_seconds": 10,
            },
        },
        message="ping",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path / "hermes",
    )

    assert result.exit_code == 0
    assert result.final_response.startswith("🟪 Clara/클라라 — OK")


def test_run_bridge_reuses_claude_session_for_same_bridge_key(tmp_path):
    fake = tmp_path / "claude"
    calls = tmp_path / "calls.jsonl"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        "calls.write_text(calls.read_text() + json.dumps(sys.argv, ensure_ascii=False) + '\\n' if calls.exists() else json.dumps(sys.argv, ensure_ascii=False) + '\\n')\n"
        "resume = '--resume' in sys.argv\n"
        "sid = 'session-2' if resume else 'session-1'\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'OK','session_id':sid}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    cfg = {
        "clara_cli": {
            "command": str(fake),
            "workdir": str(workdir),
            "allowed_tools": "Read",
            "timeout_seconds": 10,
        }
    }

    first = run_claude_code_bridge_sync(
        config=cfg,
        message="first",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path / "hermes",
        bridge_session_key="cli:test-pane",
    )
    second = run_claude_code_bridge_sync(
        config=cfg,
        message="second",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path / "hermes",
        bridge_session_key="cli:test-pane",
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    argv_lines = [json.loads(line) for line in calls.read_text().splitlines()]
    assert "--resume" not in argv_lines[0]
    assert "--resume" in argv_lines[1]
    assert argv_lines[1][argv_lines[1].index("--resume") + 1] == "session-1"
    session_map = json.loads((tmp_path / "hermes" / "runtime" / "claude-code-bridge-sessions.json").read_text())
    assert session_map["cli:test-pane"]["session_id"] == "session-2"


def test_run_bridge_config_can_disable_claude_native_resume(tmp_path):
    fake = tmp_path / "claude"
    calls = tmp_path / "calls.jsonl"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"calls = pathlib.Path({str(calls)!r})\n"
        "calls.write_text(calls.read_text() + json.dumps(sys.argv, ensure_ascii=False) + '\\n' if calls.exists() else json.dumps(sys.argv, ensure_ascii=False) + '\\n')\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'OK','session_id':'session-1'}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    cfg = {
        "clara_cli": {
            "command": str(fake),
            "workdir": str(workdir),
            "allowed_tools": "Read",
            "timeout_seconds": 10,
            "resume_enabled": False,
        }
    }

    for message in ("first", "second"):
        result = run_claude_code_bridge_sync(
            config=cfg,
            message=message,
            context_prompt=None,
            channel_prompt=None,
            history=[],
            hermes_home=tmp_path / "hermes",
            bridge_session_key="cli:test-pane",
        )
        assert result.exit_code == 0

    argv_lines = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(argv_lines) == 2
    assert "--resume" not in argv_lines[0]
    assert "--resume" not in argv_lines[1]


def test_error_max_turns_message_is_continuable_not_failure(tmp_path):
    from gateway.claude_code_bridge import _format_failure_result

    message = _format_failure_result(
        parsed={"is_error": True, "subtype": "error_max_turns"},
        stderr="",
        stdout="",
        job_id="clara-test",
        exit_code=1,
        log_dir=tmp_path,
        max_turns=100,
    )

    assert "실패한 것이 아니라 작업 제한(max_turns)에 도달" in message
    assert "이어서 진행할 수 있습니다" in message
    assert "max_turns=100" in message
    assert "작업이 실패했습니다" not in message


def test_spend_limit_message_points_to_hugo_lead_fallback(tmp_path):
    from gateway.claude_code_bridge import _format_failure_result

    message = _format_failure_result(
        parsed={
            "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
        },
        stderr="",
        stdout="",
        job_id="clara-quota",
        exit_code=1,
        log_dir=tmp_path,
        max_turns=100,
    )

    assert "월 사용 한도" in message
    assert "hermes-hugo" in message
    assert "Hugo/Codex 작업대" in message


def test_run_claude_agent_sdk_bridge_uses_sdk_transport(monkeypatch, tmp_path):
    import gateway.claude_agent_sdk_bridge as sdk_bridge

    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = {}

    def fake_run_sdk_turn(**kwargs):
        calls.update(kwargs)
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "SDK bridge response",
            "session_id": "sdk-session-1",
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "delivery": "agent-sdk",
        }

    monkeypatch.setattr(sdk_bridge, "run_sdk_turn", fake_run_sdk_turn)

    result = run_claude_agent_sdk_bridge(
        config={
            "clara_cli": {
                "enabled": True,
                "sdk_enabled": True,
                "workdir": str(workdir),
                "command": "/usr/local/bin/claude",
                "max_turns": 3,
                "sdk_tools": "claude_code",
                "sdk_setting_sources": "none",
            }
        },
        message="구현 작업으로 처리해줘",
        context_prompt="ctx",
        channel_prompt="channel",
        history=[],
        hermes_home=tmp_path / "hermes",
        bridge_session_key="thread-1",
    )

    assert result.exit_code == 0
    assert result.raw_json["delivery"] == "agent-sdk"
    assert result.final_response.startswith("🟪 Clara/클라라 — ")
    assert "SDK bridge response" in result.final_response
    assert calls["claude_bin"] == "/usr/local/bin/claude"
    assert calls["workdir"] == str(workdir)
    assert calls["tools_mode"] == "claude_code"
    assert calls["setting_sources"] == "none"
    assert "구현 작업으로 처리해줘" in calls["prompt"]
    assert "progress_callback" in calls


def test_run_claude_agent_sdk_bridge_forwards_progress_callback(monkeypatch, tmp_path):
    import gateway.claude_agent_sdk_bridge as sdk_bridge

    workdir = tmp_path / "repo"
    workdir.mkdir()
    progress_events = []

    def fake_run_sdk_turn(**kwargs):
        kwargs["progress_callback"]("sdk.stream_event", "Claude stream_event: message_start", {"message_type": "StreamEvent"})
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "OK",
            "delivery": "agent-sdk",
        }

    monkeypatch.setattr(sdk_bridge, "run_sdk_turn", fake_run_sdk_turn)

    result = run_claude_agent_sdk_bridge(
        config={"clara_cli": {"enabled": True, "sdk_enabled": True, "workdir": str(workdir)}},
        message="구현 작업으로 처리해줘",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path / "hermes",
        bridge_session_key="thread-1",
        progress_callback=lambda event_type, text, data: progress_events.append((event_type, text, data)),
    )

    assert result.exit_code == 0
    assert progress_events == [("sdk.stream_event", "Claude stream_event: message_start", {"message_type": "StreamEvent"})]


def test_sdk_progress_text_summarizes_stream_events():
    from gateway.claude_agent_sdk_bridge import _sdk_progress_text

    StreamEvent = type("StreamEvent", (), {})
    tool_event = StreamEvent()
    tool_event.event = {
        "type": "content_block_start",
        "content_block": {"type": "tool_use", "name": "Read"},
    }

    text_delta = StreamEvent()
    text_delta.event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "secret content"}}

    assert _sdk_progress_text(tool_event) == ("sdk.tool.started", "Claude tool 시작: Read")
    assert _sdk_progress_text(text_delta) == ("sdk.text_delta", "Claude 응답 작성 중")
