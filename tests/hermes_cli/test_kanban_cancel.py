from __future__ import annotations

import signal

from hermes_cli import kanban_db as kb


def test_cancel_ready_task_is_sticky_blocked_and_not_respawned(
    all_assignees_spawnable,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="cancel me",
            assignee="clive",
        )

        assert kb.cancel_task(conn, task_id, reason="scope changed")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "cancelled"
        assert run.summary == "scope changed"

        spawned = []
        kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace, **kwargs: spawned.append(task.id) or 1,
        )
        assert spawned == []
        assert kb.get_task(conn, task_id).status == "blocked"


def test_cancel_running_task_terminates_owned_pid_and_closes_run(
    all_assignees_spawnable,
    monkeypatch,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="running",
            assignee="clive",
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = 4242 WHERE id = ?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = 4242 WHERE task_id = ? AND status = 'running'",
            (task_id,),
        )
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        sent = []
        signal_observed_status = []

        def signal_worker(pid, sig):
            signal_observed_status.append(kb.get_task(conn, task_id).status)
            sent.append((pid, sig))

        assert kb.cancel_task(
            conn,
            task_id,
            reason="operator cancel",
            signal_fn=signal_worker,
        )

        assert sent == [(4242, signal.SIGTERM)]
        assert signal_observed_status == ["blocked"]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.claim_lock is None
        assert task.worker_pid is None
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "cancelled"
        assert run.status == "cancelled"
        assert run.worker_pid == 4242
        assert run.summary == "operator cancel"
        assert run.metadata and run.metadata["termination_attempted"] is True


def test_cancel_refuses_completed_task():
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="already done")
        assert kb.complete_task(conn, task_id, result="done")
        assert kb.cancel_task(conn, task_id, reason="too late") is False
        assert kb.get_task(conn, task_id).status == "done"


def test_worker_pid_and_capability_publish_only_to_current_claim():
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="publish", assignee="clive")
        claimed = kb.claim_task(conn, task_id, claimer="local:test-run")
        assert claimed is not None
        run = kb.latest_run(conn, task_id)
        assert run is not None and claimed.claim_lock

        assert kb._set_worker_pid(
            conn,
            task_id,
            4242,
            expected_run_id=run.id,
            expected_claim_lock=claimed.claim_lock,
            worker_capability="worker-only-capability",
        )
        published = kb.latest_run(conn, task_id)
        assert published is not None
        assert published.worker_pid == 4242
        assert published.worker_capability_hash

        assert kb.cancel_task(conn, task_id, reason="cancel after publication")
        assert not kb._set_worker_pid(
            conn,
            task_id,
            5252,
            expected_run_id=run.id,
            expected_claim_lock=claimed.claim_lock,
            worker_capability="late-capability",
        )
        ended = kb.latest_run(conn, task_id)
        assert ended is not None
        assert ended.worker_pid == 4242


def test_worker_capability_is_required_for_trusted_completion_attestation():
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="attest", assignee="coda")
        claimed = kb.claim_task(conn, task_id, claimer="local:coda-run")
        assert claimed is not None and claimed.claim_lock
        run = kb.latest_run(conn, task_id)
        assert run is not None
        capability = "one-time-coda-capability"
        assert kb._set_worker_pid(
            conn,
            task_id,
            4343,
            expected_run_id=run.id,
            expected_claim_lock=claimed.claim_lock,
            worker_capability=capability,
        )

        assert not kb.complete_task(
            conn,
            task_id,
            summary="PASS: wrong worker",
            metadata={"_hermes_worker_attestation": {"forged": True}},
            expected_run_id=run.id,
            expected_claim_lock=claimed.claim_lock,
            expected_worker_capability="wrong-capability",
        )
        assert kb.get_task(conn, task_id).status == "running"

        assert kb.complete_task(
            conn,
            task_id,
            summary="PASS: authenticated worker",
            metadata={"review": {"verdict": "approve"}},
            expected_run_id=run.id,
            expected_claim_lock=claimed.claim_lock,
            expected_worker_capability=capability,
        )
        completed = kb.latest_run(conn, task_id)
        assert completed is not None and completed.metadata
        attestation = completed.metadata["_hermes_worker_attestation"]
        assert attestation["run_id"] == run.id
        assert attestation["profile"] == "coda"
        assert attestation["capability_hash"] == completed.worker_capability_hash


def test_reclaim_cannot_reopen_task_cancelled_between_its_two_phases(monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="race", assignee="clive")
        assert kb.claim_task(conn, task_id) is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = 4545 WHERE id = ?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = 4545 WHERE task_id = ?",
            (task_id,),
        )
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

        def cancel_during_signal(_pid, _sig):
            assert kb.get_task(conn, task_id).status == "blocked"
            assert kb.cancel_task(conn, task_id, reason="cancel won the race")

        assert not kb.reclaim_task(
            conn,
            task_id,
            reason="operator reclaim",
            signal_fn=cancel_during_signal,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"
        assert kb.latest_run(conn, task_id).outcome == "cancelled"


def test_termination_rechecks_process_identity_before_sigkill(monkeypatch):
    monkeypatch.setattr(kb, "_claimer_id", lambda: "local:test")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    checks = iter([True, False])
    monkeypatch.setattr(
        kb,
        "_worker_process_identity_matches",
        lambda *args, **kwargs: next(checks),
    )
    monkeypatch.setattr(kb.time, "sleep", lambda _seconds: None)
    sent = []
    monkeypatch.setattr(kb.os, "killpg", lambda pid, sig: sent.append((pid, sig)))

    result = kb._terminate_reclaimed_worker(
        4646,
        "local:claim",
        task_id="t_identity",
        expected_start_time=1.0,
    )

    assert sent == [(4646, signal.SIGTERM)]
    assert result["sigkill"] is False
    assert result["terminated"] is False
