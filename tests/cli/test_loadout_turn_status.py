from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli import HermesCLI


@pytest.fixture
def status_type():
    try:
        from hermes_cli.loadout_turn_status import LoadoutTurnStatus
    except ImportError as exc:  # RED: module does not exist before the feature lands.
        pytest.fail(f"loadout turn status module is missing: {exc}")
    return LoadoutTurnStatus


def test_loadout_turn_status_exposes_all_nine_states(status_type) -> None:
    tracker = status_type(delayed_after=30.0, stalled_after=120.0)
    expected_labels = {
        "RUNNING": "🟢 RUNNING",
        "WAITING": "🔵 WAITING",
        "DELAYED": "🟡 DELAYED",
        "STALLED": "🟠 STALLED",
        "DISCONNECTED": "🔴 DISCONNECTED",
        "WAITING APPROVAL": "🟣 WAITING APPROVAL",
        "COMPLETED": "✅ COMPLETED",
        "FAILED": "❌ FAILED",
        "STOPPED": "⛔ STOPPED",
    }

    tracker.start(now=100.0, phase="Hermes 요청 처리")
    assert tracker.snapshot(now=100.0)["label"] == expected_labels["RUNNING"]

    tracker.wait(now=110.0, phase="모델 응답")
    assert tracker.snapshot(now=110.0)["label"] == expected_labels["WAITING"]
    assert tracker.snapshot(now=141.0)["label"] == expected_labels["DELAYED"]
    assert tracker.snapshot(now=231.0)["label"] == expected_labels["STALLED"]

    tracker.disconnect(now=240.0)
    assert tracker.snapshot(now=240.0)["label"] == expected_labels["DISCONNECTED"]

    tracker.start(now=250.0)
    tracker.wait_for_approval(now=250.0)
    assert tracker.snapshot(now=400.0)["label"] == expected_labels["WAITING APPROVAL"]

    tracker.start(now=410.0)
    tracker.complete(now=410.0)
    assert tracker.snapshot(now=410.0)["label"] == expected_labels["COMPLETED"]

    tracker.start(now=420.0)
    tracker.fail(now=420.0)
    assert tracker.snapshot(now=420.0)["label"] == expected_labels["FAILED"]

    tracker.start(now=430.0)
    tracker.stop(now=430.0)
    assert tracker.snapshot(now=430.0)["label"] == expected_labels["STOPPED"]

    tracker.start(now=440.0)
    tracker.progress(now=440.0, phase="tool 실행")
    assert tracker.snapshot(now=440.0)["label"] == expected_labels["RUNNING"]


def test_waiting_approval_is_not_promoted_to_delayed_or_stalled(status_type) -> None:
    tracker = status_type(delayed_after=30.0, stalled_after=120.0)
    tracker.start(now=10.0)
    tracker.wait_for_approval(now=20.0)

    assert tracker.snapshot(now=10_000.0)["status"] == "WAITING APPROVAL"


def test_loadout_status_is_launcher_scoped_and_visible_in_status_bar(status_type) -> None:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = "openai-codex/gpt-5.6-sol"
    cli_obj.session_start = datetime.now()
    cli_obj.conversation_history = []
    cli_obj.agent = None
    cli_obj.preloaded_skills = ["hermes-loadout", "hugo-crew-orchestration"]
    cli_obj._loadout_turn_status = status_type()
    cli_obj._loadout_turn_status.start(phase="Hermes 요청 처리")
    cli_obj._status_bar_visible = True
    cli_obj._model_picker_state = None
    cli_obj._get_tui_terminal_width = lambda: 160
    cli_obj._is_session_yolo_active = lambda: False

    snapshot = cli_obj._get_status_bar_snapshot()
    text = cli_obj._build_status_bar_text(width=160)
    rendered_fragments = "".join(
        fragment_text for _style, fragment_text in cli_obj._get_status_bar_fragments()
    )

    assert snapshot["loadout_status"]["status"] == "RUNNING"
    assert "🟢 RUNNING" in text
    assert "🟢 RUNNING" in rendered_fragments


def test_plain_hermes_status_bar_does_not_gain_loadout_status(status_type) -> None:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = "openai-codex/gpt-5.6-sol"
    cli_obj.session_start = datetime.now()
    cli_obj.conversation_history = []
    cli_obj.agent = None
    cli_obj.preloaded_skills = []
    cli_obj._loadout_turn_status = status_type()
    cli_obj._loadout_turn_status.start()

    snapshot = cli_obj._get_status_bar_snapshot()
    text = cli_obj._build_status_bar_text(width=160)

    assert snapshot["loadout_status"] is None
    assert "RUNNING" not in text


@pytest.mark.parametrize("width", [10, 32, 51, 52, 75, 76, 120, 200])
def test_loadout_status_fragments_never_overflow_terminal_width(
    status_type, width
) -> None:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = "openai-codex/gpt-5.6-sol"
    cli_obj.session_start = datetime.now()
    cli_obj.conversation_history = []
    cli_obj.agent = None
    cli_obj.preloaded_skills = ["hermes-loadout"]
    cli_obj._loadout_turn_status = status_type()
    cli_obj._loadout_turn_status.wait_for_approval()
    cli_obj._status_bar_visible = True
    cli_obj._model_picker_state = None
    cli_obj._get_tui_terminal_width = lambda: width
    cli_obj._is_session_yolo_active = lambda: False

    rendered = "".join(
        text for _style, text in cli_obj._get_status_bar_fragments()
    )

    assert cli_obj._status_bar_display_width(rendered) <= width


def _make_lifecycle_cli(status_type):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.preloaded_skills = ["hermes-loadout", "hugo-crew-orchestration"]
    cli_obj._loadout_turn_status = status_type()
    cli_obj._invalidate = lambda *args, **kwargs: None
    cli_obj._spinner_text = ""
    cli_obj._tool_start_time = 0.0
    cli_obj._pending_tool_info = {}
    cli_obj._last_scrollback_tool = ""
    cli_obj.tool_progress_mode = "off"
    cli_obj._approval_state = None
    cli_obj._agent_running = True
    return cli_obj


def test_live_callbacks_move_between_waiting_and_running(status_type) -> None:
    cli_obj = _make_lifecycle_cli(status_type)
    cli_obj._start_loadout_turn_status()
    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "RUNNING"

    cli_obj._on_thinking("Thinking")
    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "WAITING"

    cli_obj._on_tool_progress(
        "tool.started",
        "terminal",
        "pytest",
        {"command": "pytest"},
    )
    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "RUNNING"

    cli_obj._on_tool_progress(
        "tool.completed",
        "terminal",
        duration=1.0,
        is_error=False,
    )
    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "WAITING"


def test_late_callbacks_cannot_overwrite_stopped_status(status_type) -> None:
    cli_obj = _make_lifecycle_cli(status_type)
    cli_obj._start_loadout_turn_status()
    cli_obj._finish_loadout_turn_status(
        {"completed": False, "interrupted": True},
        interrupted=True,
        worker_alive=True,
    )

    cli_obj._on_thinking("late thinking")
    cli_obj._on_tool_progress(
        "tool.started",
        "terminal",
        "late tool",
        {"command": "true"},
    )

    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "STOPPED"


def test_active_approval_modal_overrides_live_status(status_type) -> None:
    cli_obj = _make_lifecycle_cli(status_type)
    cli_obj._start_loadout_turn_status()
    cli_obj._approval_state = {"command": "dangerous", "choices": ["once", "deny"]}

    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "WAITING APPROVAL"


def test_dead_worker_does_not_flicker_disconnected_before_result_finalization(
    status_type,
) -> None:
    cli_obj = _make_lifecycle_cli(status_type)
    cli_obj._start_loadout_turn_status()
    cli_obj._loadout_worker_thread = type(
        "DeadWorker", (), {"is_alive": lambda self: False}
    )()

    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "RUNNING"


@pytest.mark.parametrize(
    ("result", "interrupted", "worker_alive", "expected"),
    [
        ({"completed": True, "failed": False}, False, True, "COMPLETED"),
        ({"completed": False, "failed": True}, False, True, "FAILED"),
        ({"completed": False, "interrupted": True}, True, True, "STOPPED"),
        (None, False, False, "DISCONNECTED"),
    ],
)
def test_turn_result_sets_terminal_status(
    status_type, result, interrupted, worker_alive, expected
) -> None:
    cli_obj = _make_lifecycle_cli(status_type)
    cli_obj._start_loadout_turn_status()

    cli_obj._finish_loadout_turn_status(
        result,
        interrupted=interrupted,
        worker_alive=worker_alive,
    )

    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == expected


def test_chat_marks_failed_when_runtime_credentials_are_unavailable(status_type) -> None:
    cli_obj = _make_lifecycle_cli(status_type)
    cli_obj.config = {}
    cli_obj._last_turn_interrupted = False
    cli_obj._ensure_runtime_credentials = lambda: False

    assert cli_obj.chat("test") is None
    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "FAILED"


def test_chat_marks_failed_when_context_injection_is_blocked(status_type) -> None:
    cli_obj = _make_lifecycle_cli(status_type)
    cli_obj.config = {}
    cli_obj.model = "openai-codex/gpt-5.6-sol"
    cli_obj.provider = "openai-codex"
    cli_obj.base_url = ""
    cli_obj.api_key = ""
    cli_obj.conversation_history = []
    cli_obj.agent = SimpleNamespace(_config_context_length=None)
    cli_obj._active_agent_route_signature = "same"
    cli_obj._ensure_runtime_credentials = lambda: True
    cli_obj._resolve_turn_agent_config = lambda _message: {
        "signature": "same",
        "model": None,
        "runtime": None,
        "request_overrides": None,
    }
    cli_obj._init_agent = lambda **_kwargs: True
    blocked = SimpleNamespace(
        expanded=False,
        blocked=True,
        references=[],
        warnings=["blocked"],
        injected_tokens=999_999,
        message="@file:too-large",
    )

    with patch(
        "agent.context_references.preprocess_context_references",
        return_value=blocked,
    ), patch("agent.model_metadata.get_model_context_length", return_value=1_000):
        response = cli_obj.chat("@file:too-large")

    assert response == "blocked"
    assert cli_obj.conversation_history == []
    assert cli_obj._get_loadout_turn_status_snapshot()["status"] == "FAILED"
