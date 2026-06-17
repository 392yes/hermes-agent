"""Tests for the resident (warm) Claude Code CLI process pool.

These use a tiny fake ``claude`` that speaks the same stream-json protocol the
real CLI does (one ``result`` event per stdin user message), so we can verify
turn-boundary parsing, warm process reuse, respawn-after-death, timeouts, and
the transparent fallback to the classic per-turn spawn — without invoking the
real subscription CLI.
"""

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from gateway.claude_resident import ResidentClaudePool, ResidentTurnError
import gateway.claude_code_bridge as bridge


def _write_fake_claude(tmp_path: Path, body: str, name: str = "fake_claude") -> str:
    """Create an executable that ignores argv and runs the given python body."""
    emitter = tmp_path / f"{name}_emitter.py"
    emitter.write_text(textwrap.dedent(body), encoding="utf-8")
    wrapper = tmp_path / name
    wrapper.write_text(
        f'#!/bin/sh\nexec python3 "{emitter}"\n', encoding="utf-8"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(wrapper)


# Echoes the received user text back inside a result event, then loops forever.
_ECHO_BODY = """
    import sys, json
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        text = msg["message"]["content"][0]["text"]
        sys.stdout.write(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}) + "\\n")
        sys.stdout.write(json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": text, "session_id": "sess-1",
        }) + "\\n")
        sys.stdout.flush()
"""

# Never emits a result (forces a turn timeout).
_HANG_BODY = """
    import sys, time
    for line in sys.stdin:
        time.sleep(30)
"""


def _run(pool, claude_bin, key, first, follow, *, timeout=20.0):
    return pool.run_turn(
        key=key,
        workdir=os.getcwd(),
        claude_bin=claude_bin,
        extra_args=[],
        env=dict(os.environ),
        first_prompt=first,
        followup_text=follow,
        timeout=timeout,
    )


def test_resident_parses_result_and_reuses_warm_process(tmp_path):
    claude_bin = _write_fake_claude(tmp_path, _ECHO_BODY)
    pool = ResidentClaudePool()
    try:
        r1 = _run(pool, claude_bin, "k1", "FULL-FRAMING-PROMPT", "raw-msg-1")
        # First turn is a fresh process -> full framing prompt was sent.
        assert r1["result"] == "FULL-FRAMING-PROMPT"
        pid1 = pool._pool["k1"].proc.pid

        r2 = _run(pool, claude_bin, "k1", "FULL-FRAMING-PROMPT-2", "raw-msg-2")
        # Warm process -> only the followup user message is sent, no re-framing.
        assert r2["result"] == "raw-msg-2"
        pid2 = pool._pool["k1"].proc.pid
        assert pid1 == pid2, "warm turn must reuse the same process"
        assert len(pool._pool) == 1
    finally:
        pool.shutdown_all()


def test_resident_respawns_after_process_death(tmp_path):
    claude_bin = _write_fake_claude(tmp_path, _ECHO_BODY)
    pool = ResidentClaudePool()
    try:
        r1 = _run(pool, claude_bin, "k1", "first", "first")
        assert r1["result"] == "first"
        entry1 = pool._pool["k1"]
        pid1 = entry1.proc.pid
        # Simulate the process dying between turns (crash / OOM / restart).
        entry1.proc.kill()
        entry1.proc.wait(timeout=3)
        # Next turn must detect the dead process and respawn a fresh one, which
        # re-sends the full framing prompt (is_new == True again).
        r2 = _run(pool, claude_bin, "k1", "second", "ignored-followup")
        assert r2["result"] == "second"
        pid2 = pool._pool["k1"].proc.pid
        assert pid1 != pid2, "dead process must be respawned"
    finally:
        pool.shutdown_all()


def test_resident_turn_timeout_raises(tmp_path):
    claude_bin = _write_fake_claude(tmp_path, _HANG_BODY)
    pool = ResidentClaudePool()
    try:
        with pytest.raises(ResidentTurnError):
            _run(pool, claude_bin, "k1", "hello", "hello", timeout=1.0)
    finally:
        pool.shutdown_all()


def test_resident_max_processes_evicts_lru(tmp_path):
    claude_bin = _write_fake_claude(tmp_path, _ECHO_BODY)
    pool = ResidentClaudePool(max_processes=1)
    try:
        _run(pool, claude_bin, "k1", "a", "a")
        _run(pool, claude_bin, "k2", "b", "b")
        # Cap of 1 must evict the older key when a second session arrives.
        assert "k2" in pool._pool
        assert len(pool._pool) == 1
    finally:
        pool.shutdown_all()


def test_bridge_resident_disabled_delegates_to_sync(monkeypatch, tmp_path):
    sentinel = bridge.ClaudeCodeBridgeResult(
        final_response="SYNC", job_id="j", workdir=".", log_dir=".", exit_code=0
    )
    called = {}

    def fake_sync(**kwargs):
        called["yes"] = True
        return sentinel

    monkeypatch.setattr(bridge, "run_claude_code_bridge_sync", fake_sync)
    out = bridge.run_claude_code_bridge_resident(
        config={"clara_cli": {"enabled": True}},  # resident_enabled missing
        message="hi",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
        bridge_session_key="gateway:s1",
    )
    assert called.get("yes") is True
    assert out.final_response == "SYNC"


def test_bridge_resident_falls_back_to_sync_on_pool_failure(monkeypatch, tmp_path):
    sentinel = bridge.ClaudeCodeBridgeResult(
        final_response="SYNC-FALLBACK", job_id="j", workdir=".", log_dir=".", exit_code=0
    )
    monkeypatch.setattr(bridge, "run_claude_code_bridge_sync", lambda **k: sentinel)

    class _FailPool:
        def run_turn(self, **kwargs):
            raise ResidentTurnError("boom")

        def invalidate(self, key):
            pass

    import gateway.claude_resident as resident
    monkeypatch.setattr(resident, "get_pool", lambda **k: _FailPool())

    out = bridge.run_claude_code_bridge_resident(
        config={"clara_cli": {"enabled": True, "resident_enabled": True, "command": "claude"}},
        message="hi",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
        bridge_session_key="gateway:s1",
    )
    assert out.final_response == "SYNC-FALLBACK"


def test_bridge_resident_retries_zero_turn_execution_error_without_resume(monkeypatch, tmp_path):
    calls = []

    class _FlakyPool:
        def run_turn(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "num_turns": 0,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "modelUsage": {},
                    "errors": ["G?.startsWith is not a function"],
                    "session_id": "stale-session",
                }
            return {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "복구 완료",
                "session_id": "fresh-session",
            }

        def invalidate(self, key):
            calls.append({"invalidated": key})

    import gateway.claude_resident as resident
    monkeypatch.setattr(resident, "get_pool", lambda **k: _FlakyPool())
    monkeypatch.setattr(
        bridge,
        "_lookup_claude_session",
        lambda **k: "stale-session",
    )

    out = bridge.run_claude_code_bridge_resident(
        config={"clara_cli": {"enabled": True, "resident_enabled": True, "command": "claude"}},
        message="작업해줘",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
        bridge_session_key="gateway:s1",
    )

    run_calls = [c for c in calls if "extra_args" in c]
    assert len(run_calls) == 2
    assert run_calls[0]["extra_args"][:2] == ["--resume", "stale-session"]
    assert "--resume" not in run_calls[1]["extra_args"]
    assert any("invalidated" in c for c in calls)
    assert out.exit_code == 0
    assert "복구 완료" in out.final_response


def test_bridge_resident_success_formats_slack_text(monkeypatch, tmp_path):
    result_event = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "작업 완료", "session_id": "sess-xyz",
    }

    class _OkPool:
        def run_turn(self, **kwargs):
            return result_event

        def invalidate(self, key):
            pass

    import gateway.claude_resident as resident
    monkeypatch.setattr(resident, "get_pool", lambda **k: _OkPool())

    out = bridge.run_claude_code_bridge_resident(
        config={"clara_cli": {"enabled": True, "resident_enabled": True, "command": "claude", "show_job_footer": True}},
        message="작업해줘",
        context_prompt=None,
        channel_prompt=None,
        history=[],
        hermes_home=tmp_path,
        bridge_session_key="gateway:s1",
    )
    assert out.exit_code == 0
    assert out.final_response.startswith("🟪 Clara/클라라 — ")
    assert "작업 완료" in out.final_response
    assert "_Claude Code CLI job:" in out.final_response
