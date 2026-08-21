from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli import HermesCLI
from hermes_cli.loadout_auto_route import (
    APPROVAL_TARGET_ENV,
    AUTO_LOADOUT_ENV,
    AutoLoadoutDispatchError,
    apply_automatic_loadout_contract,
    dispatch_automatic_loadout_request,
)


def _skill_script(tmp_path: Path) -> Path:
    script = tmp_path / "skills" / "devops" / "hermes-loadout" / "scripts" / "orchestrate.py"
    script.parent.mkdir(parents=True)
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    return script


@pytest.mark.parametrize("value", [None, "", "0", "01", "true", "TRUE"])
def test_auto_contract_requires_exact_one(value: str | None) -> None:
    environ = {} if value is None else {AUTO_LOADOUT_ENV: value}

    assert (
        apply_automatic_loadout_contract(
            "base prompt",
            ["hermes-loadout"],
            environ=environ,
        )
        == "base prompt"
    )


def test_auto_contract_declares_four_lane_implementation_topology() -> None:
    prompt = apply_automatic_loadout_contract(
        "base prompt",
        ["hermes-loadout"],
        environ={AUTO_LOADOUT_ENV: "1"},
    )

    assert "exactly 3 leaf builders" in prompt
    assert "one parallel delegation batch" in prompt
    assert "serial shared-file integrator" in prompt


def test_plain_task_dispatches_canonical_start_without_foreground_chat(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / ".git").mkdir()
    script = _skill_script(tmp_path)
    calls = []

    def fake_spawn(argv, *, cwd, session_key, env_overrides):
        calls.append((argv, cwd, session_key, env_overrides))
        return "proc_auto_1"

    result = dispatch_automatic_loadout_request(
        "Implement the requested feature.",
        loaded_skills=["hermes-loadout", "hugo-crew-orchestration"],
        session_key="session-1",
        cwd=target,
        environ={
            AUTO_LOADOUT_ENV: "1",
            APPROVAL_TARGET_ENV: "slack:C0B49801526",
        },
        orchestrator_path=script,
        runtime_dir=tmp_path / "runtime",
        spawn_background=fake_spawn,
    )

    assert result.handled is True
    assert result.action == "start"
    assert result.session_id == "proc_auto_1"
    assert len(calls) == 1
    argv, cwd, session_key, env_overrides = calls[0]
    assert argv[1:3] == [str(script), "start"]
    assert argv[argv.index("--target") + 1] == str(target.resolve())
    assert argv[argv.index("--execution-mode") + 1] == "fast"
    assert argv[argv.index("--approval-target") + 1] == "slack:C0B49801526"
    task_file = Path(argv[argv.index("--task-file") + 1])
    assert task_file.read_text(encoding="utf-8") == "Implement the requested feature."
    assert cwd == target.resolve()
    assert session_key == "session-1"
    assert env_overrides[AUTO_LOADOUT_ENV] == "0"


def test_start_omits_approval_target_when_launcher_does_not_configure_one(
    tmp_path: Path,
) -> None:
    calls = []
    dispatch_automatic_loadout_request(
        "Implement the requested feature.",
        loaded_skills=["hermes-loadout"],
        session_key="session-1",
        cwd=tmp_path,
        environ={AUTO_LOADOUT_ENV: "1"},
        orchestrator_path=_skill_script(tmp_path),
        runtime_dir=tmp_path / "runtime",
        spawn_background=lambda argv, **kwargs: calls.append(argv) or "proc_1",
    )

    assert "--approval-target" not in calls[0]


def test_spawn_failures_are_wrapped_as_dispatch_errors(tmp_path: Path) -> None:
    with pytest.raises(
        AutoLoadoutDispatchError,
        match="could not start automatic hermes-loadout start",
    ):
        dispatch_automatic_loadout_request(
            "Implement the requested feature.",
            loaded_skills=["hermes-loadout"],
            session_key="session-1",
            cwd=tmp_path,
            environ={AUTO_LOADOUT_ENV: "1"},
            orchestrator_path=_skill_script(tmp_path),
            runtime_dir=tmp_path / "runtime",
            spawn_background=lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("spawn failed")
            ),
        )


@pytest.mark.parametrize(
    ("text", "expected_action"),
    [
        ("상태 확인", "status"),
        ("진행 상황 알려줘", "status"),
        ("현재 작업 상태 보여줘", "status"),
        ("어디까지 진행됐어?", "status"),
        ("What is the status?", "status"),
        ("show status", "status"),
        ("Could you show me the status?", "status"),
        ("Could you please show me the status?", "status"),
        ("Can you show the current status please?", "status"),
        ("Show me the status please", "status"),
        ("What's the current run progress please?", "status"),
        ("계속", "resume"),
        ("continue please", "resume"),
        ("Please continue", "resume"),
        ("Can we continue?", "resume"),
        ("Could you please continue?", "resume"),
        ("Resume the current run", "resume"),
        ("Let's resume the current run", "resume"),
        ("승인 LAP-001 선택 RETRY", "approve"),
        ("approve LAP-001 option RETRY", "approve"),
        ("Please approve LAP-001 option RETRY", "approve"),
        ("Would you please approve LAP-001 option RETRY?", "approve"),
        ("거절 LAP-001", "reject"),
        ("reject LAP-001", "reject"),
    ],
)
def test_control_inputs_never_start_a_competing_run(
    tmp_path: Path,
    text: str,
    expected_action: str,
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    script = _skill_script(tmp_path)
    spawned = []
    controlled = []

    def fake_spawn(argv, *, cwd, session_key, env_overrides):
        spawned.append(argv)
        return "proc_control"

    def fake_control(argv, *, cwd, env_overrides):
        controlled.append(argv)
        return SimpleNamespace(returncode=0, stdout="CONTROL_OK", stderr="")

    result = dispatch_automatic_loadout_request(
        text,
        loaded_skills=["hermes-loadout"],
        session_key="session-1",
        cwd=target,
        environ={AUTO_LOADOUT_ENV: "1"},
        orchestrator_path=script,
        runtime_dir=tmp_path / "runtime",
        spawn_background=fake_spawn,
        run_control=fake_control,
    )

    assert result.handled is True
    assert result.action == expected_action
    all_argv = spawned + controlled
    assert len(all_argv) == 1
    assert all_argv[0][2] == expected_action
    assert "start" not in all_argv[0][1:]


@pytest.mark.parametrize(
    "text",
    [
        "go ahead",
        "yes",
        "cancel",
        "stop",
        "ok",
        "sure",
        "sounds good",
        "Please approve this",
    ],
)
def test_ambiguous_control_replies_bypass_new_task_dispatch(
    tmp_path: Path,
    text: str,
) -> None:
    result = dispatch_automatic_loadout_request(
        text,
        loaded_skills=["hermes-loadout"],
        session_key="session-1",
        cwd=tmp_path,
        environ={AUTO_LOADOUT_ENV: "1"},
        orchestrator_path=_skill_script(tmp_path),
        runtime_dir=tmp_path / "runtime",
        spawn_background=lambda *args, **kwargs: pytest.fail("must not spawn"),
    )

    assert result.handled is False
    assert result.action == "passthrough"


@pytest.mark.parametrize(
    "message",
    [
        "[IMPORTANT: Background process proc_1 completed normally (exit code 0).]",
        '[IMPORTANT: The user has invoked the "hermes-loadout" skill, indicating they want it.]',
        '[IMPORTANT: The user has invoked the "review" skill bundle, loading 2 skills together.]',
    ],
)
def test_internal_messages_bypass_new_task_dispatch(
    tmp_path: Path,
    message: str,
) -> None:
    result = dispatch_automatic_loadout_request(
        message,
        loaded_skills=["hermes-loadout"],
        session_key="session-1",
        cwd=tmp_path,
        environ={AUTO_LOADOUT_ENV: "1"},
        orchestrator_path=_skill_script(tmp_path),
        runtime_dir=tmp_path / "runtime",
        spawn_background=lambda *args, **kwargs: pytest.fail("must not spawn"),
    )

    assert result.handled is False
    assert result.action == "passthrough"


def test_status_feature_implementation_request_remains_a_real_task(tmp_path: Path) -> None:
    calls = []
    result = dispatch_automatic_loadout_request(
        "상태 관리 기능 구현해줘",
        loaded_skills=["hermes-loadout"],
        session_key="session-1",
        cwd=tmp_path,
        environ={AUTO_LOADOUT_ENV: "1"},
        orchestrator_path=_skill_script(tmp_path),
        runtime_dir=tmp_path / "runtime",
        spawn_background=lambda argv, **kwargs: calls.append(argv) or "proc_task",
    )

    assert result.action == "start"
    assert calls[0][2] == "start"


@pytest.mark.parametrize(
    "text",
    [
        "Show a status badge in the header.",
        "Check the auth code and fix the crash.",
        "Stop leaking credentials in logs.",
        "Okay, implement the requested feature.",
        "Approval workflows need an audit trail.",
        "Reject invalid authentication tokens at the gateway.",
        "Deny unauthorized requests in the middleware.",
    ],
)
def test_control_keyword_implementation_requests_remain_real_tasks(
    tmp_path: Path,
    text: str,
) -> None:
    calls = []
    result = dispatch_automatic_loadout_request(
        text,
        loaded_skills=["hermes-loadout"],
        session_key="session-1",
        cwd=tmp_path,
        environ={AUTO_LOADOUT_ENV: "1"},
        orchestrator_path=_skill_script(tmp_path),
        runtime_dir=tmp_path / "runtime",
        spawn_background=lambda argv, **kwargs: calls.append(argv) or "proc_task",
    )

    assert result.action == "start"
    assert calls[0][2] == "start"


@pytest.mark.parametrize(
    "text",
    [
        "Implement another task.",
        "계속",
        "승인 LAP-001 선택 RETRY",
        "거절 LAP-001",
    ],
)
def test_active_run_blocks_every_competing_writer(tmp_path: Path, text: str) -> None:
    result = dispatch_automatic_loadout_request(
        text,
        loaded_skills=["hermes-loadout"],
        session_key="session-1",
        cwd=tmp_path,
        environ={AUTO_LOADOUT_ENV: "1"},
        active_run=True,
        orchestrator_path=_skill_script(tmp_path),
        runtime_dir=tmp_path / "runtime",
        spawn_background=lambda *args, **kwargs: pytest.fail("must not spawn"),
        run_control=lambda *args, **kwargs: pytest.fail("must not run control"),
    )

    assert result.handled is True
    assert result.action == "active"


def test_target_lock_blocks_writer_from_a_fresh_tui_session(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    lock_path = target / "prep" / "agent-loop" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("{}", encoding="utf-8")

    result = dispatch_automatic_loadout_request(
        "Implement another task.",
        loaded_skills=["hermes-loadout"],
        session_key="fresh-session",
        cwd=target,
        environ={AUTO_LOADOUT_ENV: "1"},
        active_run=False,
        orchestrator_path=_skill_script(tmp_path),
        runtime_dir=tmp_path / "runtime",
        spawn_background=lambda *args, **kwargs: pytest.fail("must not spawn"),
    )

    assert result.handled is True
    assert result.action == "active"
    assert "this target" in result.message


def test_default_dispatch_starts_tracked_process_with_auto_marker_disabled(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    script = _skill_script(tmp_path)
    script.write_text(
        "import argparse, os\n"
        "from pathlib import Path\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('action')\n"
        "p.add_argument('--target', required=True)\n"
        "p.add_argument('--task-file')\n"
        "p.add_argument('--execution-mode')\n"
        "p.add_argument('--approval-target')\n"
        "p.add_argument('--approval-mode')\n"
        "a=p.parse_args()\n"
        "Path(a.target, 'auto-marker.txt').write_text("
        "os.environ.get('HERMES_LOADOUT_AUTO_ORCHESTRATE', '<missing>'), "
        "encoding='utf-8')\n",
        encoding="utf-8",
    )

    result = dispatch_automatic_loadout_request(
        "Run the tracked fake orchestrator.",
        loaded_skills=["hermes-loadout"],
        session_key="session-integration",
        cwd=target,
        environ={AUTO_LOADOUT_ENV: "1"},
        orchestrator_path=script,
        runtime_dir=tmp_path / "runtime",
    )

    from tools.process_registry import process_registry

    completed = process_registry.wait(result.session_id, timeout=10)
    assert completed["status"] == "exited"
    assert completed["exit_code"] == 0
    assert (target / "auto-marker.txt").read_text(encoding="utf-8") == "0"


def test_cli_dispatch_hook_handles_task_before_chat() -> None:
    cli = HermesCLI.__new__(HermesCLI)
    cli.preloaded_skills = ["hermes-loadout", "hugo-crew-orchestration"]
    cli.session_id = "session-1"
    cli._console_print = MagicMock()
    cli._invalidate = MagicMock()
    dispatch_result = SimpleNamespace(
        handled=True,
        action="start",
        message="LOADOUT started",
        session_id="proc_1",
    )

    with patch.dict(os.environ, {AUTO_LOADOUT_ENV: "1"}), patch(
        "hermes_cli.loadout_auto_route.dispatch_automatic_loadout_request",
        return_value=dispatch_result,
    ) as dispatch:
        handled = cli._maybe_dispatch_automatic_loadout_input("Implement it")

    assert handled is True
    dispatch.assert_called_once()
    cli._console_print.assert_called_once()


def test_waiting_approval_with_exited_writer_allows_exact_control_dispatch() -> None:
    cli = HermesCLI.__new__(HermesCLI)
    cli.preloaded_skills = ["hermes-loadout"]
    cli.session_id = "session-1"
    cli._console_print = MagicMock()
    cli._invalidate = MagicMock()
    cli._get_loadout_orchestrator_status_snapshot = lambda: {
        "status": "WAITING APPROVAL",
        "terminal": False,
        "process_session_id": "proc_finished",
    }
    dispatch_result = SimpleNamespace(
        handled=True,
        action="approve",
        message="approval started",
        session_id="proc_approve",
    )

    with patch.dict(os.environ, {AUTO_LOADOUT_ENV: "1"}), patch(
        "tools.process_registry.process_registry.get",
        return_value=SimpleNamespace(exited=True),
    ), patch(
        "hermes_cli.loadout_auto_route.dispatch_automatic_loadout_request",
        return_value=dispatch_result,
    ) as dispatch:
        handled = cli._maybe_dispatch_automatic_loadout_input(
            "승인 LAP-001 선택 RETRY"
        )

    assert handled is True
    assert dispatch.call_args.kwargs["active_run"] is False


def test_process_loop_checks_dispatch_before_foreground_chat() -> None:
    source = inspect.getsource(HermesCLI.run)

    assert source.index("_maybe_dispatch_automatic_loadout_input") < source.index(
        "self.chat(user_input"
    )
