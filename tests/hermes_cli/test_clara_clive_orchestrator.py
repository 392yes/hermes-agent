from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import clara_clse_orchestrator as cc  # shared implementation module
from hermes_cli.clara_clive_orchestrator import (
    ClaraCliveOrchestrator,
    JobSpec,
    OrchestrationError,
    OwnershipConflict,
    _process_group_alive,
    _terminate_process_group,
    normalize_owned_paths,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.stdout.rstrip("\n")


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / "impl.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "lead.py").write_text("LEAD = 1\n", encoding="utf-8")
    (repo / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


@pytest.fixture
def orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaraCliveOrchestrator:
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    return ClaraCliveOrchestrator(
        runtime_root=home / "runtime" / "clara-clive-jobs",
        board="clara-clive-test",
        spawn_workers=False,
    )


def _spec(repo: Path, *, clse_files: tuple[str, ...] = ("impl.py",)) -> JobSpec:
    return JobSpec(
        repo=repo,
        title="Implement isolated change",
        task="Change VALUE from 1 to 2 and verify the file.",
        clse_files=clse_files,
        clara_files=("lead.py",),
        tests=("python -m py_compile impl.py",),
        timeout_seconds=300,
    )


def _complete_task(
    board: str,
    task_id: str,
    *,
    profile: str,
    summary: str,
    metadata: dict,
) -> None:
    with kb.connect(board=board) as conn:
        claimed = kb.claim_task(conn, task_id, claimer=profile)
        assert claimed is not None
        run = kb.latest_run(conn, task_id)
        assert run is not None
        worker_capability = f"test-worker-capability-{profile}-{run.id}"
        assert claimed.claim_lock
        assert kb._set_worker_pid(
            conn,
            task_id,
            900_000 + run.id,
            expected_run_id=run.id,
            expected_claim_lock=claimed.claim_lock,
            worker_capability=worker_capability,
        )
        assert kb.complete_task(
            conn,
            task_id,
            summary=summary,
            metadata=metadata,
            expected_run_id=run.id,
            expected_claim_lock=claimed.claim_lock,
            expected_worker_capability=worker_capability,
        )


def _finish_impl(
    orchestrator: ClaraCliveOrchestrator,
    job: dict,
    *,
    extra_change: str | None = None,
) -> dict:
    worktree = Path(job["worktree"])
    (worktree / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    if extra_change:
        (worktree / extra_change).write_text("UNOWNED = True\n", encoding="utf-8")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="Implementation complete",
        metadata={"changed_files": ["impl.py"], "tests_run": ["py_compile"]},
    )
    return orchestrator.tick(job["job_id"])


def _approve_review(orchestrator: ClaraCliveOrchestrator, job: dict) -> dict:
    _complete_task(
        orchestrator.board,
        job["review_task_id"],
        profile="coda",
        summary="PASS: scope, tests, and regression review complete",
        metadata={
            "review": {
                "verdict": "approve",
                "findings": [],
                "tested": [
                    {"command": command, "exit_code": 0}
                    for command in job["tests"]
                ],
                "patch_sha256": job["patch_sha256"],
                "base_commit": job["base_commit"],
            }
        },
    )
    return orchestrator.tick(job["job_id"])


def test_normalize_owned_paths_rejects_escape_git_and_overlap(clean_repo: Path) -> None:
    assert normalize_owned_paths(clean_repo, ["impl.py", "src/new.py"]) == (
        "impl.py",
        "src/new.py",
    )

    for bad in ("../escape.py", "/tmp/escape.py", ".git/config", "src/../../x"):
        with pytest.raises(OrchestrationError):
            normalize_owned_paths(clean_repo, [bad])

    external = clean_repo.parent / "outside"
    external.mkdir()
    (clean_repo / "linked-outside").symlink_to(external, target_is_directory=True)
    with pytest.raises(OrchestrationError, match="symlink"):
        normalize_owned_paths(clean_repo, ["linked-outside/payload.py"])

    with pytest.raises(OwnershipConflict, match="overlap"):
        JobSpec(
            repo=clean_repo,
            title="bad",
            task="bad",
            clse_files=("src",),
            clara_files=("src/view.py",),
        ).validated()


def test_dispatch_creates_detached_worktree_task_and_transactional_lock(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    first = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)

    assert first["status"] == "implementing"
    assert Path(first["worktree"]).is_dir()
    assert _git(Path(first["worktree"]), "rev-parse", "HEAD") == first["base_commit"]
    assert _git(Path(first["worktree"]), "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    with kb.connect(board=orchestrator.board) as conn:
        task = kb.get_task(conn, first["implementation_task_id"])
        assert task is not None
        assert task.assignee == "clive"
        assert task.workspace_kind == "dir"
        assert task.workspace_path == first["worktree"]
        assert task.status == "ready"

    with pytest.raises(OwnershipConflict):
        orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)

    cancelled = orchestrator.cancel(first["job_id"], reason="test cleanup")
    assert cancelled["status"] == "cancelled"
    second = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    assert second["job_id"] != first["job_id"]


def test_job_operations_reject_a_mismatched_persisted_board(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    wrong_board = ClaraCliveOrchestrator(
        runtime_root=orchestrator.runtime_root,
        board="wrong-board",
        spawn_workers=False,
    )

    for operation in (
        lambda: wrong_board.status(job["job_id"]),
        lambda: wrong_board.tick(job["job_id"]),
        lambda: wrong_board.cancel(job["job_id"], reason="must not release"),
    ):
        with pytest.raises(OrchestrationError, match="belongs to board"):
            operation()

    assert orchestrator.status(job["job_id"])["status"] == "implementing"
    with orchestrator._state_conn() as conn:
        ownership_count = conn.execute(
            "SELECT COUNT(*) FROM ownership WHERE job_id = ?", (job["job_id"],)
        ).fetchone()[0]
    assert ownership_count > 0


def test_completed_implementation_is_scoped_patched_and_handed_to_coda(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)

    assert reviewing["status"] == "reviewing"
    assert reviewing["review_task_id"].startswith("t_")
    patch = Path(reviewing["patch_path"])
    assert patch.is_file()
    text = patch.read_text(encoding="utf-8")
    assert "VALUE = 2" in text
    assert reviewing["patch_sha256"]
    with kb.connect(board=orchestrator.board) as conn:
        review = kb.get_task(conn, reviewing["review_task_id"])
        assert review is not None
        assert review.assignee == "coda"
        assert review.workspace_path == reviewing["worktree"]
        assert review.status == "ready"
        assert reviewing["implementation_task_id"] in kb.parent_ids(
            conn, reviewing["review_task_id"]
        )


def test_known_untracked_worker_runtime_artifacts_are_removed_before_scope_gate(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    worktree = Path(job["worktree"])
    (worktree / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    lifecycle = worktree / ".clara-clive-lifecycle"
    lifecycle.mkdir()
    (lifecycle / "summary.txt").write_text("done\n", encoding="utf-8")
    session_memory = worktree / ".claude" / "memory"
    session_memory.mkdir(parents=True)
    (session_memory / "last-session.json").write_text("{}\n", encoding="utf-8")
    pycache = worktree / "__pycache__"
    pycache.mkdir()
    (pycache / "impl.cpython-311.pyc").write_bytes(b"generated")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="Implementation complete",
        metadata={"changed_files": ["impl.py"]},
    )

    reviewing = orchestrator.tick(job["job_id"])
    assert reviewing["status"] == "reviewing"
    assert not lifecycle.exists()
    assert not session_memory.exists()
    assert not pycache.exists()


def test_concurrent_ticks_create_exactly_one_coda_review_card(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    worktree = Path(job["worktree"])
    (worktree / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="Implementation complete",
        metadata={"changed_files": ["impl.py"]},
    )

    original = orchestrator._tick_implementing
    rendezvous = threading.Barrier(2)

    def synchronized(row):
        rendezvous.wait(timeout=5)
        return original(row)

    monkeypatch.setattr(orchestrator, "_tick_implementing", synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: orchestrator.tick(job["job_id"]), range(2)))
    assert {result["status"] for result in results} <= {"capturing", "reviewing"}
    assert orchestrator.status(job["job_id"])["status"] == "reviewing"

    with kb.connect(board=orchestrator.board) as conn:
        review_tasks = [
            task
            for task in kb.list_tasks(conn, assignee="coda")
            if job["job_id"] in task.title
        ]
    assert len(review_tasks) == 1
    assert review_tasks[0].status == "ready"


def test_cancel_during_capture_cannot_resurrect_job_or_leave_active_review(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    worktree = Path(job["worktree"])
    (worktree / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="Implementation complete",
        metadata={"changed_files": ["impl.py"]},
    )
    original_create = orchestrator._create_review_task

    def create_then_cancel(*args, **kwargs):
        review_id = original_create(*args, **kwargs)
        cancelled = orchestrator.cancel(job["job_id"], reason="race test")
        assert cancelled["status"] == "cancelled"
        with kb.connect(board=orchestrator.board) as conn:
            orphan = kb.get_task(conn, review_id)
        assert orphan is not None and orphan.status == "blocked"
        return review_id

    monkeypatch.setattr(orchestrator, "_create_review_task", create_then_cancel)
    result = orchestrator.tick(job["job_id"])

    assert result["status"] == "cancelled"
    with orchestrator._state_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ownership WHERE job_id = ?", (job["job_id"],)
        ).fetchone()[0] == 0
    with kb.connect(board=orchestrator.board) as conn:
        active_reviews = [
            task
            for task in kb.list_tasks(conn, assignee="coda")
            if job["job_id"] in task.title and task.status not in {"blocked", "archived"}
        ]
    assert active_reviews == []


def test_stale_capture_recovery_cannot_replace_new_live_owner(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    worktree = Path(job["worktree"])
    (worktree / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="Implementation complete",
        metadata={"changed_files": ["impl.py"]},
    )
    with orchestrator._state_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'capturing', capture_token = 'old', "
            "capturing_pid = 999999, capturing_process_start = 0 "
            "WHERE job_id = ?",
            (job["job_id"],),
        )
        stale_row = orchestrator._get_job_row(conn, job["job_id"])

    original_transition = orchestrator._transition_status
    interleaved = False

    def interleave_new_owner(job_id, expected, claimed, **kwargs):
        nonlocal interleaved
        if not interleaved and expected == "capturing" and claimed == "capturing":
            interleaved = True
            assert original_transition(
                job_id,
                "capturing",
                "capturing",
                guard_columns={
                    "capture_token": "old",
                    "capturing_pid": 999999,
                    "capturing_process_start": 0,
                },
                capture_token="new-live-owner",
                capturing_pid=os.getpid(),
                capturing_process_start=cc._process_start_time(os.getpid()),
            )
        return original_transition(job_id, expected, claimed, **kwargs)

    monkeypatch.setattr(orchestrator, "_transition_status", interleave_new_owner)
    orchestrator._tick_implementing(stale_row)

    with orchestrator._state_conn() as conn:
        current = orchestrator._get_job_row(conn, job["job_id"])
    assert current["status"] == "capturing"
    assert current["capture_token"] == "new-live-owner"
    assert current["review_task_id"] is None


def test_terminal_state_cannot_be_overwritten_by_stale_failure(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    orchestrator.cancel(job["job_id"], reason="terminal first")

    assert not orchestrator._set_terminal(
        job["job_id"], "failed", error="stale failure"
    )
    assert orchestrator.status(job["job_id"])["status"] == "cancelled"


def test_completed_worker_must_exit_before_patch_capture_and_review(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    worktree = Path(job["worktree"])
    (worktree / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        with kb.connect(board=orchestrator.board) as conn:
            claimed = kb.claim_task(conn, job["implementation_task_id"])
            assert claimed is not None
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (worker.pid, job["implementation_task_id"]),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? "
                "WHERE task_id = ? AND status = 'running'",
                (worker.pid, job["implementation_task_id"]),
            )
            conn.commit()
            run = kb.latest_run(conn, job["implementation_task_id"])
            assert run is not None
            assert kb.complete_task(
                conn,
                job["implementation_task_id"],
                summary="completed before process exit",
                metadata={"changed_files": ["impl.py"]},
                expected_run_id=run.id,
            )

        still_waiting = orchestrator.tick(job["job_id"])
        assert still_waiting["status"] == "implementing"
        assert still_waiting["review_task_id"] is None
    finally:
        os.killpg(worker.pid, 15)
        worker.wait(timeout=5)

    reviewing = orchestrator.tick(job["job_id"])
    assert reviewing["status"] == "reviewing"


def test_scope_violation_fails_closed_and_releases_ownership(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    failed = _finish_impl(orchestrator, job, extra_change="other.py")

    assert failed["status"] == "failed"
    assert "SCOPE_VIOLATION" in failed["error"]
    replacement = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    assert replacement["status"] == "implementing"


def test_changed_symlink_is_rejected_before_patch_capture(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    impl = Path(job["worktree"]) / "impl.py"
    impl.unlink()
    impl.symlink_to("/tmp/clara-clive-outside")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="created symlink",
        metadata={"changed_files": ["impl.py"]},
    )

    failed = orchestrator.tick(job["job_id"])
    assert failed["status"] == "failed"
    assert "SYMLINK" in failed["error"]


def test_coda_approval_allows_patch_apply_while_preserving_clara_parallel_work(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)

    # This is the independent Clara lane and is intentionally dirty at integrate time.
    (clean_repo / "lead.py").write_text("LEAD = 2\n", encoding="utf-8")
    ready = _approve_review(orchestrator, reviewing)
    assert ready["status"] == "ready_to_apply"

    integrated = orchestrator.apply(ready["job_id"])
    assert integrated["status"] == "integrated"
    assert (clean_repo / "impl.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (clean_repo / "lead.py").read_text(encoding="utf-8") == "LEAD = 2\n"
    assert set(_git(clean_repo, "status", "--short").splitlines()) == {
        " M impl.py",
        " M lead.py",
    }


def test_live_apply_owner_is_not_reconciled_by_stale_tick(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    ready = _approve_review(orchestrator, _finish_impl(orchestrator, job))
    entered_apply = threading.Event()
    release_apply = threading.Event()
    original_git = cc._git

    def slow_apply(repo, *args):
        if args and args[0] == "apply":
            entered_apply.set()
            assert release_apply.wait(timeout=10)
        return original_git(repo, *args)

    monkeypatch.setattr(cc, "_git", slow_apply)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result_future = pool.submit(orchestrator.apply, ready["job_id"])
        assert entered_apply.wait(timeout=5)
        with orchestrator._state_conn() as conn:
            conn.execute(
                "UPDATE jobs SET updated_at = 0 WHERE job_id = ?",
                (ready["job_id"],),
            )
        assert orchestrator.tick(ready["job_id"])["status"] == "applying"
        release_apply.set()
        assert result_future.result(timeout=10)["status"] == "integrated"


def test_dead_apply_owner_recovers_without_competing_with_live_process(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    ready = _approve_review(orchestrator, _finish_impl(orchestrator, job))
    with orchestrator._state_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'applying', applying_pid = 999999, "
            "applying_process_start = 0, updated_at = 0 WHERE job_id = ?",
            (ready["job_id"],),
        )

    recovered = orchestrator.tick(ready["job_id"])
    assert recovered["status"] == "ready_to_apply"


def test_stale_apply_recovery_cannot_clobber_reacquired_live_owner(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    ready = _approve_review(orchestrator, _finish_impl(orchestrator, job))
    with orchestrator._state_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'applying', apply_token = 'old', "
            "applying_pid = 999999, applying_process_start = 0 WHERE job_id = ?",
            (ready["job_id"],),
        )
        stale_row = orchestrator._get_job_row(conn, ready["job_id"])

    original_transition = orchestrator._transition_status
    interleaved = False
    live_started = cc._process_start_time(os.getpid())

    def interleave_reacquired_owner(job_id, expected, claimed, **kwargs):
        nonlocal interleaved
        if not interleaved and expected == "applying" and claimed == "ready_to_apply":
            interleaved = True
            old_guard = {
                "apply_token": "old",
                "applying_pid": 999999,
                "applying_process_start": 0,
            }
            assert original_transition(
                job_id,
                "applying",
                "ready_to_apply",
                guard_columns=old_guard,
                apply_token=None,
                applying_pid=None,
                applying_process_start=None,
            )
            assert original_transition(
                job_id,
                "ready_to_apply",
                "applying",
                apply_token="new-live-owner",
                applying_pid=os.getpid(),
                applying_process_start=live_started,
            )
        return original_transition(job_id, expected, claimed, **kwargs)

    monkeypatch.setattr(orchestrator, "_transition_status", interleave_reacquired_owner)
    orchestrator._tick_applying(stale_row)

    with orchestrator._state_conn() as conn:
        current = orchestrator._get_job_row(conn, ready["job_id"])
    assert current["status"] == "applying"
    assert current["apply_token"] == "new-live-owner"
    assert current["applying_pid"] == os.getpid()


def test_binary_rename_patch_preserves_both_owned_path_boundaries(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    assets = clean_repo / "assets"
    assets.mkdir()
    (assets / "old.bin").write_bytes(bytes(range(64)))
    _git(clean_repo, "add", "assets/old.bin")
    _git(clean_repo, "commit", "-m", "add binary")
    spec = JobSpec(
        repo=clean_repo,
        title="binary rename",
        task="rename old.bin to new.bin",
        clse_files=("assets/old.bin", "assets/new.bin"),
        clara_files=("lead.py",),
        tests=("git diff --check",),
        timeout_seconds=300,
    )
    job = orchestrator.dispatch(spec, start_supervisor=False)
    worktree = Path(job["worktree"])
    old = worktree / "assets" / "old.bin"
    new = worktree / "assets" / "new.bin"
    old.rename(new)
    new.write_bytes(new.read_bytes() + b"\x00\xff")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="binary rename complete",
        metadata={"changed_files": ["assets/old.bin", "assets/new.bin"]},
    )
    reviewing = orchestrator.tick(job["job_id"])
    ready = _approve_review(orchestrator, reviewing)
    integrated = orchestrator.apply(ready["job_id"])

    assert integrated["status"] == "integrated"
    assert not (assets / "old.bin").exists()
    assert (assets / "new.bin").read_bytes().endswith(b"\x00\xff")


def test_apply_refuses_when_main_clive_owned_file_changed_after_dispatch(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    ready = _approve_review(orchestrator, reviewing)
    (clean_repo / "impl.py").write_text("VALUE = 99\n", encoding="utf-8")

    with pytest.raises(OwnershipConflict, match="impl.py"):
        orchestrator.apply(ready["job_id"])
    current = orchestrator.status(ready["job_id"])
    assert current["status"] == "conflict"
    assert (clean_repo / "impl.py").read_text(encoding="utf-8") == "VALUE = 99\n"


def test_apply_refuses_main_changes_outside_declared_clara_lane(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    ready = _approve_review(orchestrator, reviewing)
    (clean_repo / "other.py").write_text("OTHER = 7\n", encoding="utf-8")

    with pytest.raises(OwnershipConflict, match="other.py"):
        orchestrator.apply(ready["job_id"])
    assert orchestrator.status(ready["job_id"])["status"] == "conflict"
    assert (clean_repo / "impl.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_review_mutation_is_rejected_even_when_coda_reports_approve(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    (Path(reviewing["worktree"]) / "impl.py").write_text("VALUE = 3\n", encoding="utf-8")

    failed = _approve_review(orchestrator, reviewing)
    assert failed["status"] == "failed"
    assert "REVIEW_MUTATED_PATCH" in failed["error"]
    assert (clean_repo / "impl.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_review_gate_checks_final_git_visible_state_without_staging(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    original_git = cc._git

    def no_review_staging(repo, *args):
        if args[:2] == ("add", "-A"):
            raise AssertionError("review verification must not mutate the index")
        return original_git(repo, *args)

    monkeypatch.setattr(cc, "_git", no_review_staging)
    ready = _approve_review(orchestrator, reviewing)
    assert ready["status"] == "ready_to_apply"


def test_review_contract_describes_cooperative_git_visible_boundary(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    with kb.connect(board=orchestrator.board) as conn:
        task = kb.get_task(conn, reviewing["review_task_id"])
    assert task is not None and task.body
    assert "final Git-visible" in task.body
    assert "ignored or transient filesystem activity is not monitored" in task.body
    assert "Do not execute reviewed source code" in task.body
    assert "Static inspection plus the declared tests is sufficient" in task.body
    assert 'exact `command` and integer `exit_code: 0`' in task.body
    assert "Any content mutation fails" not in task.body


@pytest.mark.parametrize(
    "tested_evidence",
    [
        [],
        [{"command": "python -m py_compile impl.py", "exit_code": 1}],
        [{"command": "not run: python -m py_compile impl.py", "exit_code": 0}],
        [{"command": "python -m py_compile impl.py", "exit_code": True}],
    ],
)
def test_review_approval_requires_success_for_every_exact_declared_test(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    tested_evidence: list,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    _complete_task(
        orchestrator.board,
        reviewing["review_task_id"],
        profile="coda",
        summary="PASS: review without declared test evidence",
        metadata={
            "review": {
                "verdict": "approve",
                "findings": [],
                "tested": tested_evidence,
                "patch_sha256": reviewing["patch_sha256"],
                "base_commit": reviewing["base_commit"],
            }
        },
    )

    failed = orchestrator.tick(reviewing["job_id"])
    assert failed["status"] == "failed"
    assert "REVIEW_ATTESTATION_INVALID" in failed["error"]


def test_review_completion_without_explicit_approve_fails_closed(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    _complete_task(
        orchestrator.board,
        reviewing["review_task_id"],
        profile="coda",
        summary="Review completed with concerns",
        metadata={"review": {"verdict": "reject", "findings": ["bug"]}},
    )

    failed = orchestrator.tick(reviewing["job_id"])
    assert failed["status"] == "failed"
    assert "REVIEW_REJECTED" in failed["error"]
    assert (clean_repo / "impl.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_review_approve_without_patch_and_base_attestation_fails_closed(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    _complete_task(
        orchestrator.board,
        reviewing["review_task_id"],
        profile="coda",
        summary="PASS: but missing artifact attestation",
        metadata={
            "review": {
                "verdict": "approve",
                "findings": [],
                "tested": ["python -m py_compile impl.py"],
            }
        },
    )

    failed = orchestrator.tick(reviewing["job_id"])
    assert failed["status"] == "failed"
    assert "REVIEW_ATTESTATION_INVALID" in failed["error"]


def test_forged_review_from_non_coda_run_is_rejected(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    _complete_task(
        orchestrator.board,
        reviewing["review_task_id"],
        profile="clive",
        summary="PASS: forged approval",
        metadata={
            "review": {
                "verdict": "approve",
                "findings": [],
                "tested": ["git diff --check"],
                "patch_sha256": reviewing["patch_sha256"],
                "base_commit": reviewing["base_commit"],
            }
        },
    )

    failed = orchestrator.tick(reviewing["job_id"])
    assert failed["status"] == "failed"
    assert "REVIEW_ATTESTATION_INVALID" in failed["error"]


def test_cancel_blocks_ready_task_and_releases_lock(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    cancelled = orchestrator.cancel(job["job_id"], reason="scope changed")

    assert cancelled["status"] == "cancelled"
    with kb.connect(board=orchestrator.board) as conn:
        task = kb.get_task(conn, job["implementation_task_id"])
        assert task is not None
        assert task.status == "blocked"
        run = kb.latest_run(conn, task.id)
        assert run is not None
        assert run.outcome == "cancelled"
        assert "scope changed" in (run.summary or "")

    replacement = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    assert replacement["status"] == "implementing"


def test_supervisor_timeout_terminates_worker_process_group_and_cancels(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    spec = JobSpec(
        repo=clean_repo,
        title="timeout",
        task="simulate a stuck worker",
        clse_files=("impl.py",),
        clara_files=("lead.py",),
        timeout_seconds=1,
    )
    job = orchestrator.dispatch(spec, start_supervisor=False)
    task_id = job["implementation_task_id"]
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)']); time.sleep(30)"
            ),
            task_id,
        ],
        start_new_session=True,
    )
    try:
        with kb.connect(board=orchestrator.board) as conn:
            assert kb.claim_task(conn, task_id) is not None
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (worker.pid, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? "
                "WHERE task_id = ? AND status = 'running'",
                (worker.pid, task_id),
            )
            conn.commit()

        cancelled = orchestrator.supervise(job["job_id"], poll_seconds=0.1)
        assert cancelled["status"] == "cancelled"
        assert "timeout after 1s" in cancelled["error"]
        with kb.connect(board=orchestrator.board) as conn:
            task = kb.get_task(conn, task_id)
            run = kb.latest_run(conn, task_id)
        assert task is not None and task.status == "blocked"
        assert run is not None and run.outcome == "cancelled"
        assert not _process_group_alive(worker.pid)
    finally:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        worker.wait(timeout=5)


@pytest.mark.live_system_guard_bypass
def test_orphaned_process_group_descendant_can_still_be_cancelled(
    tmp_path: Path,
) -> None:
    task_id = "t_orphan_group"
    child_pid_file = tmp_path / "child.pid"
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)',sys.argv[2]]); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
            ),
            str(child_pid_file),
            task_id,
        ],
        start_new_session=True,
    )
    child_pid = None
    try:
        leader.wait(timeout=5)
        deadline = time.time() + 5
        while not child_pid_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        assert _process_group_alive(leader.pid)

        assert _terminate_process_group(
            leader.pid,
            expected_task_id=task_id,
            grace_seconds=0.2,
        )
        assert not _process_group_alive(leader.pid)
    finally:
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


def test_dirty_repo_is_rejected_before_worktree_or_task_creation(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    (clean_repo / "lead.py").write_text("LEAD = 7\n", encoding="utf-8")

    with pytest.raises(OrchestrationError, match="clean"):
        orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    assert not list(orchestrator.runtime_root.glob("worktrees/*"))


def test_status_is_json_serializable(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    assert json.loads(json.dumps(orchestrator.status(job["job_id"]))) == job


def test_json_cli_supports_task_file_positional_job_and_timeout_alias(
    clean_repo: Path,
    tmp_path: Path,
) -> None:
    home = tmp_path / "cli-home"
    runtime = home / "runtime" / "clara-clive-jobs"
    brief = tmp_path / "brief.txt"
    brief.write_text("Change VALUE from 1 to 2.\n", encoding="utf-8")
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    base = [
        sys.executable,
        "-m",
        "hermes_cli.clara_clive_orchestrator",
        "--runtime-root",
        str(runtime),
        "--board",
        "clara-clive-cli-test",
        "--no-spawn-workers",
    ]

    dispatched = subprocess.run(
        [
            *base,
            "dispatch",
            "--repo",
            str(clean_repo),
            "--title",
            "CLI contract",
            "--task-file",
            str(brief),
            "--clive-file",
            "impl.py",
            "--clara-file",
            "lead.py",
            "--test",
            "python -m py_compile impl.py",
            "--timeout",
            "300",
            "--no-supervisor",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    job = json.loads(dispatched.stdout)
    job_id = job["job_id"]
    assert job["timeout_seconds"] == 300

    status = subprocess.run(
        [*base, "status", job_id],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert json.loads(status.stdout)["job_id"] == job_id

    cancelled = subprocess.run(
        [*base, "cancel", job_id, "--reason", "CLI cleanup"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert json.loads(cancelled.stdout)["status"] == "cancelled"

    terminal_wait = subprocess.run(
        [*base, "wait", job_id, "--timeout", "5"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert terminal_wait.returncode == 4
    assert json.loads(terminal_wait.stdout)["status"] == "cancelled"

    cleaned = subprocess.run(
        [*base, "cleanup", job_id],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert json.loads(cleaned.stdout)["status"] == "cancelled"


def test_file_based_worker_lifecycle_completes_exact_pinned_run(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    worktree = Path(job["worktree"])
    lifecycle = worktree / ".clara-clive-lifecycle"
    lifecycle.mkdir()
    summary = lifecycle / "summary.txt"
    metadata = lifecycle / "metadata.json"
    summary.write_text("Implementation handoff with apostrophe: it's done\n", encoding="utf-8")
    metadata.write_text(
        json.dumps(
            {
                "changed_files": ["impl.py"],
                "commands": ["python -m py_compile impl.py"],
                "tests_run": ["py_compile: pass"],
            }
        ),
        encoding="utf-8",
    )
    with kb.connect(board=orchestrator.board) as conn:
        assert kb.claim_task(conn, job["implementation_task_id"]) is not None
        run = kb.latest_run(conn, job["implementation_task_id"])
        assert run is not None

    env = dict(os.environ)
    env.update(
        {
            "HERMES_KANBAN_TASK": job["implementation_task_id"],
            "HERMES_KANBAN_RUN_ID": str(run.id),
            "HERMES_KANBAN_BOARD": orchestrator.board,
            "HERMES_KANBAN_WORKSPACE": str(worktree),
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.clara_clive_orchestrator",
            "--runtime-root",
            str(orchestrator.runtime_root),
            "--board",
            orchestrator.board,
            "--no-spawn-workers",
            "worker-complete",
            "--summary-file",
            str(summary),
            "--metadata-file",
            str(metadata),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert json.loads(proc.stdout)["status"] == "done"
    with kb.connect(board=orchestrator.board) as conn:
        task = kb.get_task(conn, job["implementation_task_id"])
        completed = kb.latest_run(conn, job["implementation_task_id"])
    assert task is not None and task.status == "done"
    assert completed is not None
    assert "it's done" in (completed.summary or "")
    assert completed.metadata and completed.metadata["changed_files"] == ["impl.py"]


def test_lifecycle_cleanup_refuses_symlinked_claude_ancestor(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    external = tmp_path / "external"
    marker = external / "memory" / "last-session.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("preserve me", encoding="utf-8")
    (worktree / ".claude").symlink_to(external, target_is_directory=True)

    with pytest.raises(OrchestrationError, match="symlink"):
        cc._remove_worker_lifecycle_files(worktree)

    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_lifecycle_cleanup_never_removes_tracked_protocol_files(
    clean_repo: Path,
) -> None:
    lifecycle = clean_repo / ".clara-clive-lifecycle"
    lifecycle.mkdir()
    marker = lifecycle / "summary.txt"
    marker.write_text("tracked protocol fixture", encoding="utf-8")
    _git(clean_repo, "add", ".clara-clive-lifecycle/summary.txt")
    _git(clean_repo, "commit", "-m", "track lifecycle fixture")

    with pytest.raises(OrchestrationError, match="tracked"):
        cc._remove_worker_lifecycle_files(clean_repo)

    assert marker.read_text(encoding="utf-8") == "tracked protocol fixture"


def test_blocked_worker_must_exit_before_job_failure_releases_ownership(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    task_id = job["implementation_task_id"]
    with kb.connect(board=orchestrator.board) as conn:
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run = kb.latest_run(conn, task_id)
        assert run is not None
        conn.execute(
            "UPDATE task_runs SET worker_pid = 4242 WHERE id = ?",
            (run.id,),
        )
        conn.commit()
        assert kb.block_task(
            conn,
            task_id,
            reason="blocked but process still unwinding",
            expected_run_id=run.id,
        )

    monkeypatch.setattr(cc, "_process_group_alive", lambda _pid: True)
    waiting = orchestrator.tick(job["job_id"])
    assert waiting["status"] == "implementing"
    with orchestrator._state_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ownership WHERE job_id = ?", (job["job_id"],)
        ).fetchone()[0] > 0

    monkeypatch.setattr(cc, "_process_group_alive", lambda _pid: False)
    failed = orchestrator.tick(job["job_id"])
    assert failed["status"] == "failed"
    assert "IMPLEMENTATION_BLOCKED" in failed["error"]


def test_manual_claimer_cannot_forge_dispatched_coda_attestation(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    with orchestrator._state_conn() as state_conn:
        spec = json.loads(
            orchestrator._get_job_row(state_conn, job["job_id"])["spec_json"]
        )
    review_metadata = {
        "verdict": "approve",
        "findings": [],
        "tested": [
            {"command": command, "exit_code": 0}
            for command in reviewing["tests"]
        ],
        "patch_sha256": reviewing["patch_sha256"],
        "base_commit": reviewing["base_commit"],
    }
    if spec.get("review_capability"):
        review_metadata["capability"] = spec["review_capability"]

    with kb.connect(board=orchestrator.board) as conn:
        claimed = kb.claim_task(
            conn, reviewing["review_task_id"], claimer="manual-clive-claimer"
        )
        assert claimed is not None
        run = kb.latest_run(conn, reviewing["review_task_id"])
        assert run is not None
        assert kb.complete_task(
            conn,
            reviewing["review_task_id"],
            summary="PASS: forged exact approval",
            metadata={"review": review_metadata},
            expected_run_id=run.id,
        )

    failed = orchestrator.tick(job["job_id"])
    assert failed["status"] == "failed"
    assert "REVIEW_ATTESTATION_INVALID" in failed["error"]


def test_review_card_does_not_disclose_attestation_capability(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    reviewing = _finish_impl(orchestrator, job)
    with kb.connect(board=orchestrator.board) as conn:
        review = kb.get_task(conn, reviewing["review_task_id"])
    assert review is not None and review.body
    assert "Review capability" not in review.body
    assert '"capability"' not in review.body


def test_dispatch_state_failure_compensates_unpublished_implementation_card(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_update = orchestrator._update_job

    def fail_publish(conn, job_id, **fields):
        if fields.get("implementation_task_id"):
            raise RuntimeError("state publish failed")
        return original_update(conn, job_id, **fields)

    monkeypatch.setattr(orchestrator, "_update_job", fail_publish)
    with pytest.raises(RuntimeError, match="state publish failed"):
        orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)

    with kb.connect(board=orchestrator.board) as conn:
        remaining = kb.list_tasks(conn, assignee="clive")
    assert remaining
    assert all(task.status in {"blocked", "archived"} for task in remaining)


def test_review_state_failure_compensates_unpublished_review_card(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    worktree = Path(job["worktree"])
    (worktree / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="Implementation complete",
        metadata={"changed_files": ["impl.py"]},
    )
    original_transition = orchestrator._transition_status

    def fail_review_publish(job_id, expected, claimed, **kwargs):
        if expected == "capturing" and claimed == "reviewing":
            raise RuntimeError("review publish failed")
        return original_transition(job_id, expected, claimed, **kwargs)

    monkeypatch.setattr(orchestrator, "_transition_status", fail_review_publish)
    failed = orchestrator.tick(job["job_id"])
    assert failed["status"] == "failed"

    with kb.connect(board=orchestrator.board) as conn:
        reviews = [
            task
            for task in kb.list_tasks(conn, assignee="coda")
            if job["job_id"] in task.title
        ]
    assert reviews
    assert all(task.status in {"blocked", "archived"} for task in reviews)


def test_compensation_refuses_blocked_card_with_live_worker(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    task_id = job["implementation_task_id"]
    with kb.connect(board=orchestrator.board) as conn:
        assert kb.claim_task(conn, task_id) is not None
        run = kb.latest_run(conn, task_id)
        assert run is not None
        conn.execute(
            "UPDATE task_runs SET worker_pid = 4747 WHERE id = ?",
            (run.id,),
        )
        conn.commit()
        assert kb.block_task(
            conn,
            task_id,
            reason="blocked but live",
            expected_run_id=run.id,
        )

    monkeypatch.setattr(cc, "_process_group_alive", lambda _pid: True)
    monkeypatch.setattr(cc, "_terminate_process_group", lambda *args, **kwargs: False)
    with pytest.raises(OrchestrationError, match="ownership|process group"):
        orchestrator._cancel_card_or_raise(task_id, reason="compensate")


def test_review_compensation_failure_keeps_job_and_ownership_nonterminal(
    orchestrator: ClaraCliveOrchestrator,
    clean_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = orchestrator.dispatch(_spec(clean_repo), start_supervisor=False)
    worktree = Path(job["worktree"])
    (worktree / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    _complete_task(
        orchestrator.board,
        job["implementation_task_id"],
        profile="clive",
        summary="Implementation complete",
        metadata={"changed_files": ["impl.py"]},
    )
    original_transition = orchestrator._transition_status

    def fail_review_publish(job_id, expected, claimed, **kwargs):
        if expected == "capturing" and claimed == "reviewing":
            raise RuntimeError("review publish failed")
        return original_transition(job_id, expected, claimed, **kwargs)

    monkeypatch.setattr(orchestrator, "_transition_status", fail_review_publish)
    monkeypatch.setattr(
        orchestrator,
        "_cancel_card_or_raise",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OrchestrationError("compensation unavailable")
        ),
    )

    pending = orchestrator.tick(job["job_id"])
    assert pending["status"] == "capturing"
    assert "COMPENSATION_PENDING" in (pending["error"] or "")
    with orchestrator._state_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ownership WHERE job_id = ?", (job["job_id"],)
        ).fetchone()[0] > 0
