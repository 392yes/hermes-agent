from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import MagicMock, patch

from cli import HermesCLI
from hermes_cli.loadout_orchestrator_status import (
    LoadoutOrchestratorStatusMonitor,
    LoadoutRunCandidate,
    normalize_status_report,
    parse_loadout_process,
)
from hermes_cli.loadout_turn_status import LoadoutTurnStatus


def _skill_tree(tmp_path: Path) -> tuple[Path, Path]:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "devops" / "hermes-loadout"
    script = skill_dir / "scripts" / "orchestrate.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test runner\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: hermes-loadout\n---\n", encoding="utf-8"
    )
    return skills_root, script


def _candidate(tmp_path: Path, *, session_id: str = "proc_test") -> LoadoutRunCandidate:
    skills_root, script = _skill_tree(tmp_path)
    target = tmp_path / "project"
    target.mkdir()
    entry = {
        "session_id": session_id,
        "session_key": "session-1",
        "task_id": "default",
        "command": f"python3 '{script}' resume --target '{target}'",
        "cwd": str(tmp_path),
        "pid": 1234,
        "started_at": 100.0,
        "status": "running",
    }
    candidate = parse_loadout_process(
        entry,
        session_key="session-1",
        skill_roots=[skills_root],
    )
    assert candidate is not None
    return candidate


def _report(candidate: LoadoutRunCandidate, status: str = "RUNNING") -> dict:
    return {
        "run_id": "LOADOUT-1",
        "status": "running",
        "target": str(candidate.target),
        "runtime": {
            "display_status": status,
            "phase": "parent_verification",
            "phase_label": "Parent 검증",
            "cycle_display": "1/4",
            "heartbeat_age_seconds": 3,
            "heartbeat_source": "runtime",
            "parent_pid": 4321,
        },
    }


def _cli(status: LoadoutTurnStatus) -> HermesCLI:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = "openai-codex/gpt-5.6-sol"
    cli_obj.session_start = datetime.now()
    cli_obj.conversation_history = []
    cli_obj.agent = None
    cli_obj.preloaded_skills = ["hermes-loadout"]
    cli_obj._loadout_turn_status = status
    cli_obj._loadout_orchestrator_status_monitor = None
    cli_obj._approval_state = None
    cli_obj._agent_running = False
    cli_obj._status_bar_visible = True
    cli_obj._model_picker_state = None
    cli_obj._get_tui_terminal_width = lambda: 160
    cli_obj._is_session_yolo_active = lambda: False
    return cli_obj


def test_parse_loadout_process_binds_exact_session_and_target(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    assert candidate.session_id == "proc_test"
    assert candidate.session_key == "session-1"
    assert candidate.target == (tmp_path / "project").resolve()
    assert candidate.subcommand == "resume"


def test_parse_loadout_process_rejects_other_session_and_status_probe(tmp_path: Path) -> None:
    skills_root, script = _skill_tree(tmp_path)
    target = tmp_path / "project"
    target.mkdir()
    base = {
        "session_id": "proc_test",
        "session_key": "other-session",
        "task_id": "default",
        "command": f"python3 '{script}' resume --target '{target}'",
        "cwd": str(tmp_path),
        "pid": 1234,
        "started_at": 100.0,
        "status": "running",
    }

    assert parse_loadout_process(
        base,
        session_key="session-1",
        skill_roots=[skills_root],
    ) is None

    base["session_key"] = "session-1"
    base["command"] = f"python3 '{script}' status --target '{target}' --json"
    assert parse_loadout_process(
        base,
        session_key="session-1",
        skill_roots=[skills_root],
    ) is None


def test_parse_loadout_process_rejects_compound_shell_commands(tmp_path: Path) -> None:
    skills_root, script = _skill_tree(tmp_path)
    target = tmp_path / "project"
    target.mkdir()
    base = {
        "session_id": "proc_test",
        "session_key": "session-1",
        "task_id": "default",
        "cwd": str(tmp_path),
        "pid": 1234,
        "started_at": 100.0,
        "status": "running",
    }

    for suffix in (";& echo bad", "\necho bad", ">& /tmp/x", "$(echo injected)"):
        base["command"] = (
            f"python3 '{script}' resume --target '{target}' {suffix}"
        )
        assert parse_loadout_process(
            base,
            session_key="session-1",
            skill_roots=[skills_root],
        ) is None


def test_parse_loadout_process_rejects_shell_substitution_in_option_values(
    tmp_path: Path,
) -> None:
    skills_root, script = _skill_tree(tmp_path)
    target = tmp_path / "project"
    target.mkdir()
    base = {
        "session_id": "proc_test",
        "session_key": "session-1",
        "task_id": "default",
        "cwd": str(tmp_path),
        "pid": 1234,
        "started_at": 100.0,
        "status": "running",
    }

    for option in ("--mode=$(id)", "--mode=`id`"):
        base["command"] = (
            f"python3 '{script}' start --target '{target}' {option}"
        )
        assert parse_loadout_process(
            base,
            session_key="session-1",
            skill_roots=[skills_root],
        ) is None


def test_normalize_status_report_builds_canonical_footer_label(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    snapshot = normalize_status_report(_report(candidate), candidate)

    assert snapshot["source"] == "orchestrator"
    assert snapshot["status"] == "RUNNING"
    assert snapshot["label"] == (
        "🟢 LOADOUT RUNNING · Parent 검증 · heartbeat 3초 전 · 1/4"
    )
    assert snapshot["run_id"] == "LOADOUT-1"
    assert snapshot["process_session_id"] == "proc_test"


def test_monitor_refreshes_cache_without_render_time_io(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    entry = {
        "session_id": candidate.session_id,
        "session_key": candidate.session_key,
        "task_id": candidate.task_id,
        "command": (
            f"python3 '{candidate.script_path}' {candidate.subcommand} "
            f"--target '{candidate.target}'"
        ),
        "cwd": str(tmp_path),
        "pid": candidate.pid,
        "started_at": candidate.started_at,
        "status": "running",
    }
    monitor = LoadoutOrchestratorStatusMonitor(
        session_key="session-1",
        session_source=lambda: [entry],
        skill_roots=[candidate.script_path.parent.parent.parent.parent],
        probe=lambda selected: _report(selected),
        on_change=lambda: None,
    )

    monitor.refresh_once()
    with patch("pathlib.Path.read_text", side_effect=AssertionError("render file I/O")), patch(
        "subprocess.run", side_effect=AssertionError("render subprocess")
    ):
        snapshot = monitor.snapshot()

    assert snapshot is not None
    assert snapshot["status"] == "RUNNING"


def test_new_candidate_probe_failure_does_not_keep_previous_terminal_status(
    tmp_path: Path,
) -> None:
    first = _candidate(tmp_path, session_id="proc_first")
    second = LoadoutRunCandidate(
        session_id="proc_second",
        session_key=first.session_key,
        task_id=first.task_id,
        script_path=first.script_path,
        target=first.target,
        subcommand=first.subcommand,
        started_at=first.started_at + 10,
        pid=5678,
        running=True,
    )
    active = [first]

    def entries():
        return [
            {
                "session_id": candidate.session_id,
                "session_key": candidate.session_key,
                "task_id": candidate.task_id,
                "command": (
                    f"python3 '{candidate.script_path}' {candidate.subcommand} "
                    f"--target '{candidate.target}'"
                ),
                "cwd": str(tmp_path),
                "pid": candidate.pid,
                "started_at": candidate.started_at,
                "status": "running",
            }
            for candidate in active
        ]

    def probe(candidate: LoadoutRunCandidate):
        if candidate.session_id == "proc_second":
            raise TimeoutError("status unavailable")
        return _report(candidate, status="COMPLETED")

    monitor = LoadoutOrchestratorStatusMonitor(
        session_key="session-1",
        session_source=entries,
        skill_roots=[first.script_path.parent.parent.parent.parent],
        probe=probe,
        on_change=lambda: None,
    )
    monitor.refresh_once()
    assert monitor.snapshot()["status"] == "COMPLETED"

    active[:] = [second]
    monitor.refresh_once()

    snapshot = monitor.snapshot()
    assert snapshot["process_session_id"] == "proc_second"
    assert snapshot["status"] == "DELAYED"


def test_monitor_rebinds_when_cli_session_rotates(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    monitor = LoadoutOrchestratorStatusMonitor(
        session_key="session-1",
        session_source=lambda: [],
        skill_roots=[candidate.script_path.parent.parent.parent.parent],
        probe=lambda selected: _report(selected),
        on_change=lambda: None,
    )

    monitor.rebind_session("session-2")

    assert monitor.session_key == "session-2"
    assert monitor.snapshot() is None


def test_inflight_old_session_discovery_cannot_rebind_after_rotation(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    entry = {
        "session_id": candidate.session_id,
        "session_key": candidate.session_key,
        "task_id": candidate.task_id,
        "command": (
            f"python3 '{candidate.script_path}' {candidate.subcommand} "
            f"--target '{candidate.target}'"
        ),
        "cwd": str(tmp_path),
        "pid": candidate.pid,
        "started_at": candidate.started_at,
        "status": "running",
    }

    def blocked_source():
        entered.set()
        assert release.wait(timeout=2)
        return [entry]

    monitor = LoadoutOrchestratorStatusMonitor(
        session_key="session-1",
        session_source=blocked_source,
        skill_roots=[candidate.script_path.parent.parent.parent.parent],
        probe=lambda selected: _report(selected),
        on_change=lambda: None,
    )
    worker = threading.Thread(target=monitor.refresh_once)
    worker.start()
    assert entered.wait(timeout=2)

    monitor.rebind_session("session-2")
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert monitor.session_key == "session-2"
    assert monitor.snapshot() is None


def test_new_session_rebinds_existing_monitor() -> None:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = None
    cli_obj.conversation_history = []
    cli_obj._session_db = None
    cli_obj.session_id = "old-session"
    cli_obj.session_start = datetime.now()
    monitor = MagicMock()
    cli_obj._loadout_orchestrator_status_monitor = monitor

    cli_obj.new_session(silent=True)

    monitor.rebind_session.assert_called_once_with(cli_obj.session_id)


def test_active_canonical_run_overrides_completed_foreground_turn(tmp_path: Path) -> None:
    tracker = LoadoutTurnStatus()
    tracker.start(now=1.0)
    tracker.complete(now=2.0)
    cli_obj = _cli(tracker)
    candidate = _candidate(tmp_path)
    canonical = normalize_status_report(_report(candidate), candidate)
    cli_obj._loadout_orchestrator_status_monitor = SimpleNamespace(
        snapshot=lambda: canonical
    )

    snapshot = cli_obj._get_loadout_turn_status_snapshot()
    text = cli_obj._build_status_bar_text(width=160)

    assert snapshot["source"] == "orchestrator"
    assert snapshot["status"] == "RUNNING"
    assert "🟢 LOADOUT RUNNING" in text
    assert "✅ COMPLETED" not in text


def test_live_foreground_turn_temporarily_precedes_terminal_canonical_run(
    tmp_path: Path,
) -> None:
    tracker = LoadoutTurnStatus()
    tracker.start()
    tracker.wait()
    cli_obj = _cli(tracker)
    cli_obj._agent_running = True
    candidate = _candidate(tmp_path)
    canonical = normalize_status_report(_report(candidate, status="COMPLETED"), candidate)
    cli_obj._loadout_orchestrator_status_monitor = SimpleNamespace(
        snapshot=lambda: canonical
    )

    snapshot = cli_obj._get_loadout_turn_status_snapshot()

    assert snapshot["status"] == "WAITING"
    assert snapshot.get("source") != "orchestrator"


def test_canonical_footer_never_overflows_terminal_width(tmp_path: Path) -> None:
    tracker = LoadoutTurnStatus()
    tracker.start(now=1.0)
    tracker.complete(now=2.0)
    cli_obj = _cli(tracker)
    candidate = _candidate(tmp_path)
    canonical = normalize_status_report(_report(candidate), candidate)
    cli_obj._loadout_orchestrator_status_monitor = SimpleNamespace(
        snapshot=lambda: canonical
    )

    for width in (10, 32, 51, 52, 75, 76, 120, 200):
        cli_obj._get_tui_terminal_width = lambda width=width: width
        rendered = "".join(
            text for _style, text in cli_obj._get_status_bar_fragments()
        )
        assert cli_obj._status_bar_display_width(rendered) <= width
        if width >= 16:
            assert "LOADOUT" in rendered
