from __future__ import annotations

from dataclasses import replace
import subprocess
import sys

import pytest


def _make_task(kb, *, assignee: str):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_LEAD_MODE", "clara-lead")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openai-codex")
    monkeypatch.setenv("HERMES_MODEL", "parent-model")
    monkeypatch.setenv("HERMES_PROVIDER", "parent-provider")
    monkeypatch.setenv("TEST_API_KEY", "must-not-leak")
    monkeypatch.setenv("GH_TOKEN", "must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("HERMES_SESSION_ID", "parent-session")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "slack")
    monkeypatch.setenv("DATABASE_URL", "postgresql://credential@example.invalid/db")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", '{"auths":{"example.invalid":{}}}')
    monkeypatch.setenv("GIT_DIR", "/tmp/foreign-git-dir")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/foreign-worktree")
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/foreign-index")

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(
        _make_task(kb, assignee="elias"),
        str(workspace),
        board="clara-clive-test",
    )

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    for parent_override in (
        "HERMES_LEAD_MODE",
        "HERMES_INFERENCE_PROVIDER",
        "HERMES_MODEL",
        "HERMES_PROVIDER",
    ):
        assert parent_override not in captured["env"]
    for secret_name in (
        "TEST_API_KEY",
        "GH_TOKEN",
        "SSH_AUTH_SOCK",
        "HERMES_SESSION_ID",
        "HERMES_SESSION_SOURCE",
        "DATABASE_URL",
        "DOCKER_AUTH_CONFIG",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
    ):
        assert secret_name not in captured["env"]
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]


def test_clara_clive_bridge_worker_bypasses_lead_pinning_launcher(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "clive"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("toolsets: []\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
model:
  provider: claude-code-cli
claude_code_cli:
  enabled: true
platform_toolsets:
  cli:
    - file
    - terminal
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    captured = {}

    class FakeProc:
        pid = 5151

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(
        _make_task(kb, assignee="clive"),
        str(workspace),
        board="clara-clive",
    )

    assert captured["cmd"][:3] == [sys.executable, "-m", "hermes_cli.main"]
    assert captured["env"]["HERMES_HOME"] == str(root)
    assert captured["env"]["HERMES_PROFILE"] == "clive"


def test_clara_clive_worker_refuses_missing_workspace(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "clive"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("toolsets: []\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("Popen must not run without the pinned workspace")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    missing = tmp_path / "missing-worktree"
    with pytest.raises(RuntimeError, match="workspace"):
        kb._default_spawn(
            _make_task(kb, assignee="clive"),
            str(missing),
            board="clara-clive",
        )

    actual = tmp_path / "actual-worktree"
    expected = tmp_path / "expected-worktree"
    actual.mkdir()
    expected.mkdir()
    mismatched_task = replace(
        _make_task(kb, assignee="clive"), workspace_path=str(expected)
    )
    with pytest.raises(RuntimeError, match="does not match"):
        kb._default_spawn(
            mismatched_task,
            str(actual),
            board="clara-clive",
        )


@pytest.mark.parametrize("board", ["project", "Clara-Clive-Custom"])
def test_named_clive_profile_is_isolated_independent_of_board_spelling(
    monkeypatch,
    tmp_path,
    board,
):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "clive"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("toolsets: []\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        "model:\n  provider: claude-code-cli\nclaude_code_cli:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("TEST_API_KEY", "must-not-leak")
    monkeypatch.setenv("DATABASE_URL", "postgresql://credential@example.invalid/db")

    from hermes_cli import kanban_db as kb

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(
        _make_task(kb, assignee="clive"),
        str(workspace),
        board=board,
    )

    assert "TEST_API_KEY" not in captured["env"]
    assert "DATABASE_URL" not in captured["env"]
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["cmd"][:3] == [sys.executable, "-m", "hermes_cli.main"]
