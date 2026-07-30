import json
from pathlib import Path

import pytest

from gateway import claude_code_bridge
from gateway.claude_code_bridge import build_claude_prompt, resolve_workdir


def test_opus_alias_resolves_to_opus_5():
    assert claude_code_bridge.resolve_clara_model_alias("opus") == "claude-opus-5"
    assert claude_code_bridge.resolve_clara_model_alias("opus-5") == "claude-opus-5"


def test_builder_profile_ignores_interactive_model_swap(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    claude_code_bridge.write_clara_model_override("claude-fable-5")

    builder = claude_code_bridge.bridge_config(
        {
            "clara_cli": {
                "role_mode": "senior-builder",
                "model": "claude-opus-5",
            }
        }
    )
    lead = claude_code_bridge.bridge_config(
        {"clara_cli": {"role_mode": "lead", "model": "claude-opus-5"}}
    )

    assert builder["model"] == "claude-opus-5"
    assert lead["model"] == "claude-fable-5"


def test_clive_bridge_forwards_opus_5_and_max_effort(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "CLIVE_RUNTIME_OK",
                    "session_id": "session-clive",
                }
            ),
            "",
            0,
        )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(claude_code_bridge, "_run_claude_subprocess", fake_run)
    result = claude_code_bridge.run_claude_code_bridge_sync(
        config={
            "model": {"provider": "claude-code-cli"},
            "clara_cli": {
                "command": "/fake/claude",
                "model": "claude-opus-5",
                "effort": "max",
                "role_mode": "senior-builder",
                "workdir": str(tmp_path),
                "resume_enabled": False,
            },
        },
        message="Return OK",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
    )

    assert captured["args"][captured["args"].index("--model") + 1] == "claude-opus-5"
    assert captured["args"][captured["args"].index("--effort") + 1] == "max"
    metadata = json.loads((Path(result.log_dir) / "metadata.json").read_text())
    assert metadata["model"] == "claude-opus-5"
    assert metadata["effort"] == "max"


def test_senior_builder_prompt_uses_clive_identity_and_handoff_contract():
    prompt = build_claude_prompt(
        message="Implement the scoped change and run tests.",
        role_mode="senior-builder",
        workdir="/tmp/project",
    )

    assert "You are Clive, Sangkun Lee's Claude Code Senior Builder under Clara." in prompt
    assert "You are Clara/클라라" not in prompt
    assert "Clara owns product architecture, task decomposition" in prompt
    assert "Coda/Codex owns the separate QA" in prompt
    assert "Working directory: /tmp/project" in prompt
    assert "Current user request:\nImplement the scoped change and run tests." in prompt
    assert "CLI output instruction" in prompt
    assert "Return a Hermes CLI-ready response." in prompt
    assert "Slack-ready Clive" not in prompt


def test_builder_aliases_all_select_the_clive_role():
    for role_mode in ("builder", "senior_builder", "clive", "clse"):
        prompt = build_claude_prompt(message="Work", role_mode=role_mode)
        assert "You are Clive" in prompt
        assert "review, testing, and security gate" not in prompt


def test_builder_slack_prompt_keeps_clive_identity():
    prompt = build_claude_prompt(
        message="Report status.",
        role_mode="senior-builder",
        channel_prompt="Use the Clive role marker.",
    )

    assert "Use the Clive role marker." in prompt
    assert "Identify as Clive, Claude Code Senior Builder under Clara" in prompt
    assert "Return a Slack-ready Clive response." in prompt
    assert "Return a Slack-ready Clara response." not in prompt


def test_clara_lead_prompt_remains_clara_after_builder_support():
    prompt = build_claude_prompt(
        message="Lead this task.",
        role_mode="clara-lead",
        channel_prompt="Slack #office",
    )

    assert "You are Clara/클라라, Sangkun Lee's lead orchestrator" in prompt
    assert "You are Clive" not in prompt
    assert "🟪 Clara/클라라" in prompt
    assert "Return a Slack-ready Clara response." in prompt
    assert "Return a Slack-ready Clive response." not in prompt


def test_clara_lead_prompt_requires_real_parallel_clive_dispatch_contract():
    prompt = build_claude_prompt(
        message="Implement two independent modules.",
        role_mode="clara-lead",
        workdir="/tmp/project",
    )

    assert "clara-clive dispatch" in prompt
    assert "--task-file" in prompt
    assert "outside the target repository" in prompt
    assert "Never interpolate task text into shell arguments" in prompt
    assert "continue Clara-owned work before waiting" in prompt
    assert "clara-clive wait" in prompt
    assert "separate Hermes profile `clive`" in prompt
    assert "Coda" in prompt
    assert "Do not simulate dispatch" in prompt


def test_builder_kanban_prompt_explains_cli_lifecycle_for_external_bridge(
    monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_builder123")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "clara-clive")

    prompt = build_claude_prompt(
        message="work kanban task t_builder123",
        role_mode="senior-builder",
        workdir="/tmp/project",
    )

    assert "Hermes `kanban_*` model tools are not exposed" in prompt
    assert "clara-clive worker-complete" in prompt
    assert "clara-clive worker-block" in prompt
    assert "--summary-file" in prompt
    assert "never interpolate handoff text into a shell command" in prompt
    assert "Do not exit before one lifecycle command succeeds" in prompt


def test_builder_presentation_uses_clive_progress_and_job_identity():
    builder = claude_code_bridge._bridge_presentation(
        {"role_mode": "senior-builder"}
    )
    clara = claude_code_bridge._bridge_presentation({"role_mode": "lead"})

    assert builder == {
        "actor": "Clive",
        "progress_marker": "🟧 Clive",
        "job_prefix": "clive",
    }
    assert clara == {
        "actor": "Clara",
        "progress_marker": "🟪 Clara/클라라",
        "job_prefix": "clara",
    }
    assert claude_code_bridge._new_bridge_job_id(
        {"role_mode": "senior-builder"}
    ).startswith("clive-")


def test_kanban_workspace_has_priority_over_message_and_bridge_config(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "task-worktree"
    explicit = tmp_path / "message-workdir"
    configured = tmp_path / "configured-workdir"
    for directory in (workspace, explicit, configured):
        directory.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_builder123")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    config = {"gateway": {"claude_code": {"workdir": str(configured)}}}

    assert resolve_workdir(config, f"Work in {explicit}") == str(workspace)


def test_kanban_worker_refuses_missing_or_invalid_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_builder123")
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    with pytest.raises(RuntimeError, match="HERMES_KANBAN_WORKSPACE"):
        resolve_workdir({}, "Work in /tmp")

    monkeypatch.setenv(
        "HERMES_KANBAN_WORKSPACE", str(tmp_path / "missing-worktree")
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        resolve_workdir({}, "Work in /tmp")


def test_kanban_bridge_environment_drops_parent_routing_and_credentials(
    monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_builder123")
    for key, value in {
        "HERMES_SESSION_ID": "parent-session",
        "HERMES_SESSION_SOURCE": "slack",
        "DATABASE_URL": "postgresql://credential@example.invalid/db",
        "DOCKER_AUTH_CONFIG": "credential",
        "GIT_DIR": "/tmp/foreign-git-dir",
        "GIT_WORK_TREE": "/tmp/foreign-worktree",
        "GIT_INDEX_FILE": "/tmp/foreign-index",
    }.items():
        monkeypatch.setenv(key, value)

    safe = claude_code_bridge._safe_env()
    for key in (
        "HERMES_SESSION_ID",
        "HERMES_SESSION_SOURCE",
        "DATABASE_URL",
        "DOCKER_AUTH_CONFIG",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
    ):
        assert key not in safe
