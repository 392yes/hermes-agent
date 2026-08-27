"""Clara → Clive automatic parallel orchestration coordinator.

Thin coordinator that lets Clara split substantial coding work, run the
separate ``clive`` Hermes profile asynchronously in a detached git worktree,
gate the result through a ``vera`` review card, and integrate only a
conflict-free reviewed binary patch back into the main checkout.

Durable queueing, worker launch, task/run logging, retry, and timeout
enforcement are deliberately delegated to the existing Hermes Kanban
(:mod:`hermes_cli.kanban_db`). This module owns only:

* exact file ownership (dedicated SQLite registry, transactional)
* clean detached worktree lifecycle
* scope validation and staged binary patch capture inside the worktree
* the Vera review gate (verdict + patch immutability proof)
* fingerprint/apply-check guarded integration into the main checkout
* a JSON CLI contract: dispatch / status / wait / cancel / cleanup / supervise

Safety invariants (fail closed):

* dispatch requires a clean main checkout; nothing is created otherwise
* overlapping (exact or prefix) path ownership between active jobs is rejected
* unowned worktree changes or worktree HEAD drift abort integration
* the captured patch is immutable — any final Git-visible worktree/index or
  patch-file divergence (even with an "approve" verdict) fails the job
* apply verifies main-checkout fingerprints of Clive-owned paths, then
  ``git apply --check``, then applies; the coordinator itself applies only the
  declared patch

This is a trusted-agent concurrency boundary, not an OS security sandbox.
Worktree scope checks, restricted Claude Bash tools, credential-env scrubbing,
and process-group quiescence prevent or detect accidental violations, but a
malicious same-user process could still address paths outside the worktree.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from hermes_cli import kanban_db as kb

DEFAULT_BOARD = "clara-clive"
DEFAULT_TIMEOUT_SECONDS = 3600
RUNTIME_DIRNAME = "clara-clive-jobs"

PIPELINE_STATUSES = (
    "implementing", "capturing", "reviewing", "ready_to_apply", "applying",
)
ACTIVE_STATUSES = (*PIPELINE_STATUSES, "cancelling")
TERMINAL_STATUSES = ("integrated", "failed", "cancelled", "conflict")

_ABSENT = "__absent__"

# Env vars that must never leak from the orchestrator session into detached
# supervisor / profile-worker processes: they would override the worker
# profile's own runtime, provider, or lead-mode pinning.
_SCRUB_ENV_PREFIXES = (
    "HERMES_LEAD",
    "HERMES_ORCHESTRATOR",
    "HERMES_CLARA",
    "HERMES_PROVIDER",
    "HERMES_MODEL",
    "HERMES_REASONING",
    "HERMES_SESSION_ID",
    "ANTHROPIC_MODEL",
)


class OrchestrationError(RuntimeError):
    """Base error for coordinator contract violations."""


class CompensationPending(OrchestrationError):
    """A card could not be compensated; ownership must remain held."""


class OwnershipConflict(OrchestrationError):
    """Overlapping path ownership, or an integration-time ownership breach."""


# ---------------------------------------------------------------------------
# Path ownership normalization
# ---------------------------------------------------------------------------

def normalize_owned_paths(repo: Path, paths: Iterable[str]) -> "tuple[str, ...]":
    """Normalize declared owned paths to sorted, repo-relative POSIX paths.

    Rejects (``OrchestrationError``): empty paths, absolute paths, any
    ``..``/``.``/empty segment (traversal), backslashes/NUL, and anything
    touching ``.git``. Paths need not exist yet — new files are ownable.
    """
    repo = Path(repo)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        text = str(raw).strip()
        if not text:
            raise OrchestrationError("owned path cannot be empty")
        if "\x00" in text or "\\" in text:
            raise OrchestrationError(f"unsafe characters in owned path: {raw!r}")
        if text.startswith("/") or Path(text).is_absolute():
            raise OrchestrationError(f"owned path must be repo-relative: {raw!r}")
        parts = text.strip("/").split("/")
        for part in parts:
            if part in ("", ".", ".."):
                raise OrchestrationError(f"path traversal rejected: {raw!r}")
            if part == ".git":
                raise OrchestrationError(f".git is never ownable: {raw!r}")
        joined = "/".join(parts)
        # Belt-and-suspenders: with '..' rejected the join cannot escape,
        # but verify containment anyway so future edits stay safe.
        candidate = os.path.normpath(os.path.join(str(repo), joined))
        if not (candidate == str(repo) or candidate.startswith(str(repo) + os.sep)):
            raise OrchestrationError(f"owned path escapes repository: {raw!r}")
        # A symlink component could route "inside" ownership to content
        # outside the repository (or to an unowned path). Fail closed on any
        # existing symlink along the owned path, leaf included.
        probe = repo
        for part in parts:
            probe = probe / part
            if probe.is_symlink():
                raise OrchestrationError(
                    f"owned path traverses a symlink ({part}): {raw!r}"
                )
        if joined not in seen:
            seen.add(joined)
            normalized.append(joined)
    return tuple(sorted(normalized))


def _paths_overlap(a: str, b: str) -> bool:
    # macOS commonly uses a case-insensitive filesystem and may surface the
    # same Unicode filename in NFC or NFD form. Ownership is conservative:
    # spellings that can resolve to the same path are treated as overlapping.
    left = unicodedata.normalize("NFC", a).casefold()
    right = unicodedata.normalize("NFC", b).casefold()
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


# ---------------------------------------------------------------------------
# Job specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobSpec:
    """Declarative contract for one Clara→Clive parallel job."""

    repo: Path
    title: str
    task: str
    clse_files: "tuple[str, ...]"
    clara_files: "tuple[str, ...]" = ()
    tests: "tuple[str, ...]" = ()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def validated(self) -> "JobSpec":
        """Return a normalized copy or raise on contract violations."""
        repo = Path(self.repo).expanduser()
        if not repo.is_dir():
            raise OrchestrationError(f"repo directory does not exist: {repo}")
        repo = repo.resolve(strict=True)
        if not str(self.title).strip():
            raise OrchestrationError("title is required")
        if not str(self.task).strip():
            raise OrchestrationError("task is required")
        clse = normalize_owned_paths(repo, self.clse_files)
        clara = normalize_owned_paths(repo, self.clara_files)
        if not clse:
            raise OrchestrationError("at least one Clive-owned path is required")
        overlaps = [
            f"{b} (overlaps {a})"
            for a in clse
            for b in clara
            if _paths_overlap(a, b)
        ]
        if overlaps:
            raise OwnershipConflict(
                "Clive/Clara ownership overlap: " + ", ".join(sorted(overlaps))
            )
        timeout = int(self.timeout_seconds)
        if timeout <= 0:
            raise OrchestrationError("timeout_seconds must be positive")
        return replace(
            self,
            repo=repo,
            title=str(self.title).strip(),
            task=str(self.task).strip(),
            clse_files=clse,
            clara_files=clara,
            tests=tuple(str(t) for t in self.tests),
            timeout_seconds=timeout,
        )


# ---------------------------------------------------------------------------
# Git / hashing helpers
# ---------------------------------------------------------------------------

def _git_run(
    cwd: Path,
    *args: str,
    check: bool = True,
) -> "subprocess.CompletedProcess[bytes]":
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise OrchestrationError(
            f"git {' '.join(args)} failed in {cwd} (rc={proc.returncode}): {stderr}"
        )
    return proc


def _git(cwd: Path, *args: str) -> str:
    return _git_run(cwd, *args).stdout.decode("utf-8", "replace").strip()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _iter_owned_files(repo: Path, owned: Sequence[str]) -> "list[str]":
    """Expand owned entries into the repo-relative files they currently cover."""
    files: list[str] = []
    for rel in owned:
        target = repo / rel
        if target.is_file() or target.is_symlink():
            files.append(rel)
        elif target.is_dir():
            for child in sorted(target.rglob("*")):
                if ".git" in child.parts:
                    continue
                if child.is_file() or child.is_symlink():
                    files.append(child.relative_to(repo).as_posix())
    return files


def _fingerprint_owned(repo: Path, owned: Sequence[str]) -> "dict[str, str]":
    """Map every file currently under the owned paths to its content hash.

    Absent entries are recorded with a sentinel so that a file appearing
    later (or disappearing) is detected as a mismatch at apply time.
    """
    prints: dict[str, str] = {}
    covered_files = _iter_owned_files(repo, owned)
    for rel in covered_files:
        target = repo / rel
        try:
            prints[rel] = _sha256_file(target)
        except OSError:
            prints[rel] = _ABSENT
    for rel in owned:
        target = repo / rel
        if not target.exists() and not target.is_symlink():
            prints[rel] = _ABSENT
    return prints


def _changed_paths(worktree: Path) -> "list[str]":
    """Repo-relative paths with any working-tree change (incl. untracked)."""
    out = _git_run(
        worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    ).stdout.decode("utf-8", "surrogateescape")
    entries = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        if not entry:
            i += 1
            continue
        code = entry[:2]
        paths.append(entry[3:].rstrip("/"))
        if code and code[0] in ("R", "C"):
            # In -z format the origin path follows as its own entry.
            i += 1
            if i < len(entries) and entries[i]:
                paths.append(entries[i].rstrip("/"))
        i += 1
    return paths


def _is_owned(path: str, owned: Sequence[str]) -> bool:
    p = path.rstrip("/")
    return any(_paths_overlap(p, o) and (p == o or p.startswith(o + "/")) for o in owned)


def _changed_symlink_paths(root: Path, paths: Sequence[str]) -> "list[str]":
    """Return changed paths whose leaf or an existing ancestor is a symlink."""
    unsafe: list[str] = []
    for rel in paths:
        probe = root
        for part in Path(rel).parts:
            probe = probe / part
            if probe.is_symlink():
                unsafe.append(rel)
                break
    return sorted(set(unsafe))


def _remove_worker_lifecycle_files(worktree: Path) -> None:
    """Remove known untracked worker/runtime artifacts before diff capture.

    These paths are produced by the lifecycle protocol, Claude session hooks,
    or Python itself rather than by the implementation. Tracked files are
    never removed, so a repository that intentionally versions one of these
    paths remains protected by the normal ownership check.
    """
    def untracked(path: Path) -> bool:
        rel = path.relative_to(worktree).as_posix()
        return _git_run(
            worktree, "ls-files", "--error-unmatch", "--", rel, check=False
        ).returncode != 0

    def reject_symlink_component(path: Path, label: str) -> None:
        probe = worktree
        for part in path.relative_to(worktree).parts:
            probe = probe / part
            if probe.is_symlink():
                raise OrchestrationError(f"{label} must not traverse a symlink: {probe}")

    lifecycle = worktree / ".clara-clive-lifecycle"
    reject_symlink_component(lifecycle, "worker lifecycle directory")
    if lifecycle.exists():
        tracked = _git(
            worktree, "ls-files", "--", ".clara-clive-lifecycle"
        ).strip()
        if tracked:
            raise OrchestrationError(
                "worker lifecycle directory contains tracked files; refusing cleanup"
            )
        shutil.rmtree(lifecycle)

    session_marker = worktree / ".claude" / "memory" / "last-session.json"
    reject_symlink_component(session_marker, "Claude session marker")
    if session_marker.is_file() and untracked(session_marker):
        session_marker.unlink()
        for parent in (session_marker.parent, session_marker.parent.parent):
            with contextlib.suppress(OSError):
                parent.rmdir()

    for cache_dir in worktree.rglob("__pycache__"):
        reject_symlink_component(cache_dir, "Python cache directory")
        if cache_dir.is_symlink() or not cache_dir.is_dir():
            continue
        for artifact in cache_dir.iterdir():
            if artifact.is_file() and not artifact.is_symlink() and untracked(artifact):
                artifact.unlink()
        with contextlib.suppress(OSError):
            cache_dir.rmdir()


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _process_start_time(pid: int) -> Optional[float]:
    """Return a PID birth marker so apply ownership survives PID reuse."""
    try:
        import psutil  # type: ignore

        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def _pid_identity_alive(pid: Optional[int], started_at: Optional[float]) -> bool:
    if not _pid_alive(pid):
        return False
    if pid is None or started_at is None:
        # Unknown birth marker: fail closed and assume a live owner.
        return True
    current = _process_start_time(int(pid))
    return current is None or abs(current - float(started_at)) < 0.001


def _process_group_alive(pid: Optional[int]) -> bool:
    """Whether a dispatcher-created worker process group still has members."""
    if not pid or pid <= 0:
        return False
    # Reap children owned by this process before probing so a completed leader
    # is not mistaken for a live writer merely because it is still a zombie.
    with contextlib.suppress(Exception):
        kb.reap_worker_zombies()
    if os.name != "posix" or not hasattr(os, "killpg"):
        return _pid_alive(pid)
    try:
        os.killpg(int(pid), 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _process_group_commands(pgid: int) -> "list[str]":
    """Return full command lines for members of one POSIX process group."""
    probe = subprocess.run(
        ["ps", "eww", "-axo", "pid=,pgid=,command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return []
    commands: list[str] = []
    for line in probe.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            member_pgid = int(parts[1])
        except ValueError:
            continue
        if member_pgid == int(pgid):
            commands.append(parts[2])
    return commands


def _terminate_process_group(
    pid: Optional[int], *, expected_task_id: str, grace_seconds: float = 3.0
) -> bool:
    """Terminate one verified Kanban worker process tree and confirm quiescence.

    `_default_spawn` uses ``start_new_session=True``, making the worker PID the
    process-group id. Refuse to signal if that identity no longer matches or the
    command line does not mention the expected task — this avoids a reused PID
    killing an unrelated process.
    """
    if not pid or pid <= 0 or not _process_group_alive(pid):
        return True
    if os.name != "posix" or not hasattr(os, "killpg"):
        return False
    try:
        # The leader may already have exited while descendants remain. Verify
        # the task identity on any surviving group member, then signal the PGID.
        commands = _process_group_commands(int(pid))
        if not commands or not any(expected_task_id in cmd for cmd in commands):
            return False
        if _pid_alive(pid) and os.getpgid(int(pid)) != int(pid):
            return False
        os.killpg(int(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return not _process_group_alive(pid)

    deadline = time.monotonic() + max(0.1, grace_seconds)
    while time.monotonic() < deadline:
        if not _process_group_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_group_alive(pid):
            return True
        time.sleep(0.1)
    return not _process_group_alive(pid)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class ClaraClseOrchestrator:
    """Owns job state, path ownership, worktrees, patch flow, and the CLI."""

    def __init__(
        self,
        *,
        runtime_root: Optional[Path] = None,
        board: str = DEFAULT_BOARD,
        spawn_workers: bool = True,
        implementation_profile: str = "clive",
        review_profile: str = "vera",
    ) -> None:
        if runtime_root is None:
            runtime_root = kb.kanban_home() / "runtime" / RUNTIME_DIRNAME
        self.runtime_root = Path(runtime_root)
        self.board = board
        self.spawn_workers = bool(spawn_workers)
        self.implementation_profile = implementation_profile
        self.review_profile = review_profile
        self.worktrees_root = self.runtime_root / "worktrees"
        self.patches_root = self.runtime_root / "patches"
        self.logs_root = self.runtime_root / "logs"
        for directory in (
            self.runtime_root,
            self.worktrees_root,
            self.patches_root,
            self.logs_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._state_db_path = self.runtime_root / "state.db"
        self._init_state_db()

    # -- state registry -----------------------------------------------------

    def _state_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._state_db_path), timeout=30.0)
        conn.isolation_level = None  # explicit BEGIN IMMEDIATE transactions
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        with contextlib.suppress(sqlite3.Error):
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_state_db(self) -> None:
        with contextlib.closing(self._state_conn()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    board TEXT NOT NULL,
                    status TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    repo_key TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    fingerprints_json TEXT NOT NULL DEFAULT '{}',
                    base_commit TEXT NOT NULL DEFAULT '',
                    worktree TEXT NOT NULL DEFAULT '',
                    implementation_task_id TEXT,
                    review_task_id TEXT,
                    patch_path TEXT,
                    patch_sha256 TEXT,
                    supervisor_pid INTEGER,
                    capture_token TEXT,
                    capturing_pid INTEGER,
                    capturing_process_start REAL,
                    apply_token TEXT,
                    applying_pid INTEGER,
                    applying_process_start REAL,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ownership (
                    repo_key TEXT NOT NULL,
                    path TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    PRIMARY KEY (repo_key, path, job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ownership_repo
                    ON ownership(repo_key);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(jobs)")
            }
            if "applying_pid" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN applying_pid INTEGER")
            if "applying_process_start" not in columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN applying_process_start REAL"
                )
            if "capture_token" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN capture_token TEXT")
            if "capturing_pid" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN capturing_pid INTEGER")
            if "capturing_process_start" not in columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN capturing_process_start REAL"
                )
            if "apply_token" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN apply_token TEXT")

    @contextlib.contextmanager
    def _txn(self, conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _get_job_row(self, conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise OrchestrationError(f"unknown job: {job_id}")
        if row["board"] != self.board:
            raise OrchestrationError(
                f"job {job_id} belongs to board {row['board']!r}, not {self.board!r}"
            )
        return row

    def _update_job(
        self, conn: sqlite3.Connection, job_id: str, **columns: Any
    ) -> None:
        allowed = {
            "status", "worktree", "implementation_task_id", "review_task_id",
            "patch_path", "patch_sha256", "supervisor_pid", "error",
            "base_commit", "fingerprints_json", "applying_pid",
            "applying_process_start", "apply_token", "capture_token",
            "capturing_pid", "capturing_process_start",
        }
        unknown = set(columns) - allowed
        if unknown:
            raise ValueError(f"unknown job columns: {sorted(unknown)}")
        columns["updated_at"] = int(time.time())
        assignments = ", ".join(f"{name} = ?" for name in columns)
        cur = conn.execute(
            f"UPDATE jobs SET {assignments} WHERE job_id = ? AND board = ?",
            (*columns.values(), job_id, self.board),
        )
        if cur.rowcount != 1:
            raise OrchestrationError(
                f"job {job_id} is missing or belongs to a different board"
            )

    def _release_ownership(self, conn: sqlite3.Connection, job_id: str) -> None:
        conn.execute("DELETE FROM ownership WHERE job_id = ?", (job_id,))

    def _transition_status(
        self,
        job_id: str,
        expected: str | Sequence[str],
        claimed: str,
        *,
        guard_columns: Optional[dict[str, Any]] = None,
        **columns: Any,
    ) -> bool:
        """CAS a state transition; only its winner may release ownership."""
        expected_values = (expected,) if isinstance(expected, str) else tuple(expected)
        if not expected_values:
            return False
        allowed = {
            "review_task_id", "patch_path", "patch_sha256", "error",
            "capture_token", "capturing_pid", "capturing_process_start",
            "apply_token", "applying_pid", "applying_process_start",
        }
        guards = dict(guard_columns or {})
        unknown = (set(columns) | set(guards)) - allowed
        if unknown:
            raise ValueError(f"unknown transition columns: {sorted(unknown)}")
        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [claimed, int(time.time())]
        for name, value in columns.items():
            assignments.append(f"{name} = ?")
            values.append(value)
        placeholders = ", ".join("?" for _ in expected_values)
        guard_sql = "".join(f" AND {name} IS ?" for name in guards)
        with contextlib.closing(self._state_conn()) as conn:
            with self._txn(conn):
                cur = conn.execute(
                    f"UPDATE jobs SET {', '.join(assignments)} "
                    f"WHERE job_id = ? AND board = ? "
                    f"AND status IN ({placeholders}){guard_sql}",
                    (
                        *values,
                        job_id,
                        self.board,
                        *expected_values,
                        *guards.values(),
                    ),
                )
                won = cur.rowcount == 1
                if won and claimed in TERMINAL_STATUSES:
                    self._release_ownership(conn, job_id)
                return won

    def _claim_status(self, job_id: str, expected: str, claimed: str) -> bool:
        """CAS one pipeline transition so concurrent wait/supervisor ticks join."""
        return self._transition_status(job_id, expected, claimed)

    def _set_terminal(
        self,
        job_id: str,
        status: str,
        *,
        error: Optional[str] = None,
        expected: Optional[Sequence[str]] = None,
        guard_columns: Optional[dict[str, Any]] = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"not a terminal status: {status}")
        return self._transition_status(
            job_id,
            tuple(expected or (*PIPELINE_STATUSES, "cancelling")),
            status,
            guard_columns=guard_columns,
            error=error,
            capture_token=None,
            capturing_pid=None,
            capturing_process_start=None,
            apply_token=None,
            applying_pid=None,
            applying_process_start=None,
        )

    def _fail(
        self,
        job_id: str,
        error: str,
        *,
        expected: Sequence[str] = PIPELINE_STATUSES,
        guard_columns: Optional[dict[str, Any]] = None,
    ) -> bool:
        return self._set_terminal(
            job_id,
            "failed",
            error=error,
            expected=expected,
            guard_columns=guard_columns,
        )

    # -- payload ------------------------------------------------------------

    def status(self, job_id: str) -> "dict[str, Any]":
        """Return the canonical JSON-serializable view of a job."""
        with contextlib.closing(self._state_conn()) as conn:
            row = self._get_job_row(conn, job_id)
        spec = json.loads(row["spec_json"])
        return {
            "job_id": row["job_id"],
            "board": row["board"],
            "status": row["status"],
            "title": spec["title"],
            "repo": row["repo"],
            "worktree": row["worktree"],
            "base_commit": row["base_commit"],
            "implementation_task_id": row["implementation_task_id"],
            "review_task_id": row["review_task_id"],
            "patch_path": row["patch_path"],
            "patch_sha256": row["patch_sha256"],
            "clive_files": list(spec["clse_files"]),
            "clara_files": list(spec["clara_files"]),
            "tests": list(spec["tests"]),
            "timeout_seconds": int(spec["timeout_seconds"]),
            "supervisor_pid": row["supervisor_pid"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # -- dispatch -----------------------------------------------------------

    def _cancel_card_or_raise(self, task_id: str, *, reason: str) -> None:
        """Compensate an unpublished Kanban card before ownership is released."""
        with contextlib.closing(kb.connect(board=self.board)) as conn:
            task = kb.get_task(conn, task_id)
            if task is None:
                return
            run = kb.latest_run(conn, task_id)
            worker_pid = run.worker_pid if run and run.worker_pid else task.worker_pid
            if task.status not in {"done", "archived"} and not kb.cancel_task(
                conn, task_id, reason=reason
            ):
                raise OrchestrationError(
                    f"card compensation lost concurrent state for {task_id}"
                )
            refreshed = kb.latest_run(conn, task_id)
            if refreshed and refreshed.worker_pid:
                worker_pid = refreshed.worker_pid
            if _process_group_alive(worker_pid):
                _terminate_process_group(worker_pid, expected_task_id=task_id)
            if _process_group_alive(worker_pid):
                raise OrchestrationError(
                    f"card compensation process group for {task_id} is still live; "
                    "ownership retained"
                )

    def dispatch(
        self, spec: JobSpec, *, start_supervisor: Optional[bool] = None
    ) -> "dict[str, Any]":
        """Validate, lock ownership, create the worktree + Clive card, return."""
        if not isinstance(spec, JobSpec):
            raise TypeError("dispatch requires a JobSpec")
        spec = spec.validated()
        repo = Path(spec.repo)
        repo_key = os.path.realpath(str(repo))

        if _git(repo, "rev-parse", "--is-inside-work-tree") != "true":
            raise OrchestrationError(f"not a git work tree: {repo}")
        toplevel = os.path.realpath(_git(repo, "rev-parse", "--show-toplevel"))
        if toplevel != repo_key:
            raise OrchestrationError(
                f"repo must be the git toplevel: {repo} (toplevel: {toplevel})"
            )
        common_dir = Path(_git(repo, "rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = repo / common_dir
        # All worktrees of one repository share this identity. This prevents
        # two checkout spellings from bypassing the ownership registry.
        repo_key = os.path.realpath(str(common_dir))
        dirty = _git(repo, "status", "--porcelain")
        if dirty:
            raise OrchestrationError(
                "repository must be clean before dispatch; dirty entries:\n" + dirty
            )
        base_commit = _git(repo, "rev-parse", "HEAD")
        fingerprints = _fingerprint_owned(repo, spec.clse_files)

        job_id = "ccj_" + uuid.uuid4().hex[:12]
        now = int(time.time())
        spec_json = json.dumps(
            {
                "repo": str(repo),
                "title": spec.title,
                "task": spec.task,
                "clse_files": list(spec.clse_files),
                "clara_files": list(spec.clara_files),
                "tests": list(spec.tests),
                "timeout_seconds": spec.timeout_seconds,
            }
        )

        # Transactional acquire: conflict check and lock insert are one
        # BEGIN IMMEDIATE transaction — two racing dispatchers serialize here.
        with contextlib.closing(self._state_conn()) as conn:
            with self._txn(conn):
                active_placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
                active = conn.execute(
                    "SELECT o.path, o.job_id FROM ownership o "
                    "JOIN jobs j ON j.job_id = o.job_id "
                    f"WHERE o.repo_key = ? AND j.status IN ({active_placeholders})",
                    (repo_key, *ACTIVE_STATUSES),
                ).fetchall()
                requested = list(spec.clse_files) + list(spec.clara_files)
                for held in active:
                    for want in requested:
                        if _paths_overlap(held["path"], want):
                            raise OwnershipConflict(
                                f"path {want!r} conflicts with {held['path']!r} "
                                f"held by active job {held['job_id']}"
                            )
                conn.execute(
                    "INSERT INTO jobs (job_id, board, status, repo, repo_key, "
                    "spec_json, fingerprints_json, base_commit, created_at, "
                    "updated_at) VALUES (?, ?, 'implementing', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id, self.board, str(repo), repo_key, spec_json,
                        json.dumps(fingerprints), base_commit, now, now,
                    ),
                )
                for lane, paths in (
                    ("clive", spec.clse_files),
                    ("clara", spec.clara_files),
                ):
                    for path in paths:
                        conn.execute(
                            "INSERT INTO ownership (repo_key, path, lane, job_id) "
                            "VALUES (?, ?, ?, ?)",
                            (repo_key, path, lane, job_id),
                        )

        worktree = self.worktrees_root / job_id
        impl_task_id: Optional[str] = None
        try:
            _git(
                repo, "worktree", "add", "--detach",
                str(worktree), base_commit,
            )
            impl_task_id = self._create_implementation_task(
                spec, job_id=job_id, worktree=worktree
            )
            with contextlib.closing(self._state_conn()) as conn:
                with self._txn(conn):
                    self._update_job(
                        conn, job_id,
                        worktree=str(worktree),
                        implementation_task_id=impl_task_id,
                    )
        except BaseException as exc:
            if impl_task_id is not None:
                try:
                    self._cancel_card_or_raise(
                        impl_task_id,
                        reason="implementation card state publication failed",
                    )
                except Exception as compensation_exc:
                    with contextlib.closing(self._state_conn()) as state_conn:
                        with self._txn(state_conn):
                            self._update_job(
                                state_conn,
                                job_id,
                                error=(
                                    "DISPATCH_COMPENSATION_PENDING: "
                                    f"{compensation_exc}"
                                ),
                            )
                    raise OrchestrationError(
                        "implementation card compensation failed; ownership retained"
                    ) from compensation_exc
            self._set_terminal(job_id, "failed", error=f"DISPATCH_FAILED: {exc}")
            self._remove_worktree(repo, worktree)
            raise

        if self.spawn_workers:
            self._try_spawn_workers()
        if start_supervisor is None:
            start_supervisor = self.spawn_workers
        if start_supervisor:
            self._start_supervisor(job_id)
        return self.status(job_id)

    def _create_implementation_task(
        self, spec: JobSpec, *, job_id: str, worktree: Path
    ) -> str:
        body = "\n".join(
            [
                f"Clara→Clive parallel job `{job_id}`.",
                "",
                "## Task",
                spec.task,
                "",
                "## Contract (mandatory)",
                f"- Work ONLY inside this worktree: {worktree}",
                "- You may create/modify ONLY these exact paths "
                "(repo-relative):",
                *[f"  - {p}" for p in spec.clse_files],
                "- Do NOT run `git commit`, `git push`, checkout, rebase, or "
                "any branch mutation. Leave changes uncommitted in the "
                "working tree.",
                "- Do NOT touch the main checkout, other paths, secrets, or "
                "remote services.",
                "",
                "## Verification to run before completing",
                *(
                    [f"  - {t}" for t in spec.tests]
                    if spec.tests
                    else ["  - (none declared)"]
                ),
                "",
                "## Completion",
                "Complete this card with a summary plus metadata "
                '`{"changed_files": [...], "commands": [...], '
                '"tests_run": [...]}` via the '
                "native `kanban_complete` tool. If native kanban tools are "
                "unavailable, write `.clara-clive-lifecycle/summary.txt` and "
                "`.clara-clive-lifecycle/metadata.json`, then run:",
                "  clara-clive worker-complete --summary-file "
                ".clara-clive-lifecycle/summary.txt --metadata-file "
                ".clara-clive-lifecycle/metadata.json",
                "If you cannot finish within scope, block the card with the "
                "native tool or `.clara-clive-lifecycle/reason.txt` plus "
                "`clara-clive worker-block --reason-file "
                ".clara-clive-lifecycle/reason.txt`.",
            ]
        )
        with contextlib.closing(kb.connect(board=self.board)) as conn:
            return kb.create_task(
                conn,
                title=f"[clara-clive {job_id}] {spec.title}",
                body=body,
                assignee=self.implementation_profile,
                created_by="clara",
                workspace_kind="dir",
                workspace_path=str(worktree),
                max_runtime_seconds=spec.timeout_seconds,
                idempotency_key=f"clara-clive-implementation:{job_id}",
                board=self.board,
            )

    def _try_spawn_workers(self) -> None:
        """Ask the Kanban dispatcher to launch profile workers. Best-effort."""
        try:
            with contextlib.closing(kb.connect(board=self.board)) as conn:
                kb.dispatch_once(conn, board=self.board, max_spawn=1)
        except Exception:
            # The durable card stays 'ready'; a daemon dispatcher or a later
            # supervise() pass can still launch the worker.
            pass

    # -- pipeline advancement ----------------------------------------------

    def tick(self, job_id: str) -> "dict[str, Any]":
        """Advance the job one step if its Kanban cards changed state."""
        with contextlib.closing(self._state_conn()) as conn:
            row = self._get_job_row(conn, job_id)
        if row["status"] in ("implementing", "capturing"):
            self._tick_implementing(row)
        elif row["status"] == "reviewing":
            self._tick_reviewing(row)
        elif row["status"] == "applying":
            self._tick_applying(row)
        return self.status(job_id)

    def _tick_implementing(self, row: sqlite3.Row) -> None:
        job_id = row["job_id"]
        task_id = row["implementation_task_id"]
        stage_guard = (
            {
                "capture_token": row["capture_token"],
                "capturing_pid": row["capturing_pid"],
                "capturing_process_start": row["capturing_process_start"],
            }
            if row["status"] == "capturing"
            else None
        )
        with contextlib.closing(kb.connect(board=self.board)) as conn:
            task = kb.get_task(conn, task_id) if task_id else None
            run = kb.latest_run(conn, task_id) if task_id else None
        if task is None:
            self._fail(
                job_id,
                f"IMPLEMENTATION_TASK_MISSING: {task_id}",
                expected=(row["status"],),
                guard_columns=stage_guard,
            )
            return
        if (
            task.status in {"blocked", "done"}
            and run is not None
            and _process_group_alive(run.worker_pid)
        ):
            return
        if task.status == "blocked":
            detail = (run.summary if run and run.summary else "no reason recorded")
            self._fail(
                job_id,
                f"IMPLEMENTATION_BLOCKED: {detail}",
                expected=(row["status"],),
                guard_columns=stage_guard,
            )
            return
        if task.status != "done":
            return
        capture_guard: dict[str, Any]
        if row["status"] == "implementing":
            capture_token = secrets.token_urlsafe(24)
            capture_pid = os.getpid()
            if not self._transition_status(
                job_id,
                "implementing",
                "capturing",
                capture_token=capture_token,
                capturing_pid=capture_pid,
                capturing_process_start=_process_start_time(capture_pid),
            ):
                return
            with contextlib.closing(self._state_conn()) as state_conn:
                row = self._get_job_row(state_conn, job_id)
        else:
            old_guard = {
                "capture_token": row["capture_token"],
                "capturing_pid": row["capturing_pid"],
                "capturing_process_start": row["capturing_process_start"],
            }
            if _pid_identity_alive(
                row["capturing_pid"], row["capturing_process_start"]
            ):
                return
            capture_token = secrets.token_urlsafe(24)
            capture_pid = os.getpid()
            if not self._transition_status(
                job_id,
                "capturing",
                "capturing",
                guard_columns=old_guard,
                capture_token=capture_token,
                capturing_pid=capture_pid,
                capturing_process_start=_process_start_time(capture_pid),
            ):
                return
            with contextlib.closing(self._state_conn()) as state_conn:
                row = self._get_job_row(state_conn, job_id)
        capture_guard = {
            "capture_token": row["capture_token"],
            "capturing_pid": row["capturing_pid"],
            "capturing_process_start": row["capturing_process_start"],
        }
        try:
            _remove_worker_lifecycle_files(Path(row["worktree"]))
            self._capture_patch_and_open_review(row, capture_guard=capture_guard)
        except CompensationPending as exc:
            with contextlib.closing(self._state_conn()) as state_conn:
                with self._txn(state_conn):
                    self._update_job(
                        state_conn,
                        job_id,
                        error=f"COMPENSATION_PENDING: {exc}",
                    )
        except Exception as exc:
            self._fail(
                job_id,
                f"PATCH_CAPTURE_FAILED: {exc}",
                expected=("capturing",),
                guard_columns=capture_guard,
            )

    def _capture_patch_and_open_review(
        self, row: sqlite3.Row, *, capture_guard: dict[str, Any]
    ) -> None:
        job_id = row["job_id"]
        spec = json.loads(row["spec_json"])
        worktree = Path(row["worktree"])
        owned = list(spec["clse_files"])

        head = _git(worktree, "rev-parse", "HEAD")
        if head != row["base_commit"]:
            self._fail(
                job_id,
                f"WORKTREE_HEAD_DRIFT: worktree HEAD {head} != base "
                f"{row['base_commit']} (commits are forbidden in the worktree)",
                expected=("capturing",),
                guard_columns=capture_guard,
            )
            return
        changed = _changed_paths(worktree)
        unowned = sorted({p for p in changed if not _is_owned(p, owned)})
        if unowned:
            self._fail(
                job_id,
                "SCOPE_VIOLATION: unowned worktree changes: " + ", ".join(unowned),
                expected=("capturing",),
                guard_columns=capture_guard,
            )
            return
        unsafe_symlinks = _changed_symlink_paths(worktree, changed)
        if unsafe_symlinks:
            self._fail(
                job_id,
                "SYMLINK_SCOPE_VIOLATION: changed path traverses or creates a "
                "symlink: " + ", ".join(unsafe_symlinks),
                expected=("capturing",),
                guard_columns=capture_guard,
            )
            return
        if not changed:
            self._fail(
                job_id,
                "EMPTY_PATCH: implementation produced no changes",
                expected=("capturing",),
                guard_columns=capture_guard,
            )
            return

        # Stage only inside the disposable worktree, then capture a binary
        # cached diff against the dispatch base commit.
        _git(worktree, "add", "-A")
        patch_bytes = _git_run(worktree, "diff", "--cached", "--binary").stdout
        if not patch_bytes.strip():
            self._fail(
                job_id,
                "EMPTY_PATCH: staged diff is empty",
                expected=("capturing",),
                guard_columns=capture_guard,
            )
            return
        patch_path = self.patches_root / f"{job_id}.patch"
        patch_path.write_bytes(patch_bytes)
        patch_sha = _sha256_bytes(patch_bytes)

        review_task_id = self._create_review_task(
            row, spec, patch_path=patch_path, patch_sha=patch_sha
        )
        try:
            published = self._transition_status(
                job_id,
                "capturing",
                "reviewing",
                guard_columns=capture_guard,
                review_task_id=review_task_id,
                patch_path=str(patch_path),
                patch_sha256=patch_sha,
                capture_token=None,
                capturing_pid=None,
                capturing_process_start=None,
            )
        except BaseException:
            try:
                self._cancel_card_or_raise(
                    review_task_id,
                    reason="review card state publication raised an exception",
                )
            except Exception as compensation_exc:
                raise CompensationPending(
                    f"review card {review_task_id}: {compensation_exc}"
                ) from compensation_exc
            raise
        if not published:
            try:
                self._cancel_card_or_raise(
                    review_task_id,
                    reason="parent Clara-Clive job left capturing state",
                )
            except Exception as compensation_exc:
                raise CompensationPending(
                    f"review card {review_task_id}: {compensation_exc}"
                ) from compensation_exc
            return
        if self.spawn_workers:
            self._try_spawn_workers()

    def _create_review_task(
        self,
        row: sqlite3.Row,
        spec: "dict[str, Any]",
        *,
        patch_path: Path,
        patch_sha: str,
    ) -> str:
        job_id = row["job_id"]
        body = "\n".join(
            [
                f"Independent Vera review gate for Clara→Clive job `{job_id}`.",
                "",
                "## What to review",
                f"- Worktree (read-only): {row['worktree']}",
                f"- Captured patch: {patch_path} (sha256 {patch_sha})",
                f"- Task: {spec['task']}",
                "- Declared Clive-owned paths: " + ", ".join(spec["clse_files"]),
                "- Declared tests: "
                + (", ".join(spec["tests"]) if spec["tests"] else "(none)"),
                "",
                "## Rules (mandatory)",
                "- READ-ONLY: do not edit, stage, commit, or clean anything in "
                "the worktree. Any final Git-visible source/index mutation "
                "fails the job; ignored or transient filesystem activity is not "
                "monitored by this cooperative-agent boundary.",
                "- Review scope compliance, correctness, regressions, and "
                "security. Re-run every declared test read-only. `tested` must "
                "contain one object per declared test with the exact `command` "
                "and integer `exit_code: 0`; block if any test cannot run or "
                "returns nonzero.",
                "- Do not execute reviewed source code or add optional smoke "
                "checks. Do not use Python exec/eval/compile, heredocs, process "
                "substitution, or a final compound verification script. Static "
                "inspection plus the declared tests is sufficient; once those "
                "pass, complete the review immediately.",
                "",
                "## Completion",
                "Complete this card with summary 'PASS: ...' or 'FAIL: ...' "
                "and metadata "
                '`{"review": {"verdict": "approve"|"reject", '
                '"findings": [], "tested": [{"command": "<exact declared '
                'command>", "exit_code": 0}], '
                f'"patch_sha256": "{patch_sha}", '
                f'"base_commit": "{row["base_commit"]}"}}}}`. '
                "Only verdict=approve integrates the patch.",
            ]
        )
        with contextlib.closing(kb.connect(board=self.board)) as conn:
            return kb.create_task(
                conn,
                title=f"[clara-clive {job_id}] Vera review: {spec['title']}",
                body=body,
                assignee=self.review_profile,
                created_by="clara",
                workspace_kind="dir",
                workspace_path=row["worktree"],
                parents=[row["implementation_task_id"]],
                max_runtime_seconds=int(spec["timeout_seconds"]),
                idempotency_key=f"clara-clive-review:{job_id}",
                board=self.board,
            )

    def _tick_reviewing(self, row: sqlite3.Row) -> None:
        job_id = row["job_id"]
        review_id = row["review_task_id"]
        with contextlib.closing(kb.connect(board=self.board)) as conn:
            task = kb.get_task(conn, review_id) if review_id else None
            run = kb.latest_run(conn, review_id) if review_id else None
        if task is None:
            self._fail(
                job_id,
                f"REVIEW_TASK_MISSING: {review_id}",
                expected=("reviewing",),
            )
            return
        if (
            task.status in {"blocked", "done"}
            and run is not None
            and _process_group_alive(run.worker_pid)
        ):
            return
        if task.status == "blocked":
            detail = (run.summary if run and run.summary else "no reason recorded")
            self._fail(
                job_id, f"REVIEW_BLOCKED: {detail}", expected=("reviewing",)
            )
            return
        if task.status != "done":
            return

        # Patch immutability proof comes FIRST: an approve verdict on a
        # mutated worktree or patch file must still fail closed.
        worktree = Path(row["worktree"])
        patch_path = Path(row["patch_path"]) if row["patch_path"] else None
        if patch_path is None or not patch_path.is_file():
            self._fail(
                job_id,
                f"PATCH_FILE_TAMPERED: missing patch {patch_path}",
                expected=("reviewing",),
            )
            return
        if _sha256_file(patch_path) != row["patch_sha256"]:
            self._fail(
                job_id,
                "PATCH_FILE_TAMPERED: patch file no longer matches sha256",
                expected=("reviewing",),
            )
            return
        head = _git(worktree, "rev-parse", "HEAD")
        if head != row["base_commit"]:
            self._fail(
                job_id,
                f"WORKTREE_HEAD_DRIFT: worktree HEAD {head} != base "
                f"{row['base_commit']} after review",
                expected=("reviewing",),
            )
            return
        recomputed = _git_run(worktree, "diff", "--cached", "--binary").stdout
        unstaged = _git_run(worktree, "diff", "--binary").stdout
        untracked = _git(
            worktree, "ls-files", "--others", "--exclude-standard"
        )
        if (
            _sha256_bytes(recomputed) != row["patch_sha256"]
            or bool(unstaged.strip())
            or bool(untracked.strip())
        ):
            self._fail(
                job_id,
                "REVIEW_MUTATED_PATCH: worktree content changed after patch "
                "capture; the captured patch is immutable",
                expected=("reviewing",),
            )
            return

        verdict = None
        review = None
        worker_attestation = None
        meta = run.metadata if run else None
        if isinstance(meta, dict):
            review = meta.get("review")
            worker_attestation = meta.get("_hermes_worker_attestation")
            if isinstance(review, dict):
                verdict = review.get("verdict")
        if verdict != "approve":
            self._fail(
                job_id,
                f"REVIEW_REJECTED: vera verdict={verdict!r} "
                f"(summary: {(run.summary or '').strip()[:200] if run else ''})",
                expected=("reviewing",),
            )
            return
        summary = (run.summary or "").strip() if run else ""
        spec = json.loads(row["spec_json"])
        tested = review.get("tested") if isinstance(review, dict) else None
        declared_tests = list(spec.get("tests", []))
        declared_tests_attested = (
            isinstance(tested, list)
            and len(tested) == len(declared_tests)
            and all(
                isinstance(item, dict)
                and item.get("command") == command
                and type(item.get("exit_code")) is int
                and item["exit_code"] == 0
                for item, command in zip(tested, declared_tests)
            )
        )
        attested = (
            run is not None
            and run.profile == self.review_profile
            and run.worker_pid is not None
            and bool(run.worker_capability_hash)
            and isinstance(worker_attestation, dict)
            and worker_attestation.get("run_id") == run.id
            and worker_attestation.get("profile") == self.review_profile
            and secrets.compare_digest(
                str(worker_attestation.get("capability_hash") or ""),
                str(run.worker_capability_hash or ""),
            )
            and summary.startswith("PASS:")
            and isinstance(review, dict)
            and review.get("findings") == []
            and declared_tests_attested
            and review.get("patch_sha256") == row["patch_sha256"]
            and review.get("base_commit") == row["base_commit"]
        )
        if not attested:
            self._fail(
                job_id,
                "REVIEW_ATTESTATION_INVALID: approval must come from the vera "
                "dispatcher worker run with verified capability, PASS summary, "
                "empty findings, exact per-command exit_code=0 test evidence, "
                "and exact patch_sha256/base_commit",
                expected=("reviewing",),
            )
            return
        self._claim_status(job_id, "reviewing", "ready_to_apply")

    # -- integration --------------------------------------------------------

    def _tick_applying(self, row: sqlite3.Row) -> None:
        """Reconcile an apply process that died after claiming the transition."""
        owner_pid = row["applying_pid"]
        owner_started = row["applying_process_start"]
        apply_guard = {
            "apply_token": row["apply_token"],
            "applying_pid": owner_pid,
            "applying_process_start": owner_started,
        }
        if owner_pid is None:
            # Legacy/unknown owner: never guess that it is dead.
            return
        if _pid_identity_alive(int(owner_pid), owner_started):
            return
        job_id = row["job_id"]
        repo = Path(row["repo"])
        patch_path = Path(row["patch_path"] or "")
        if not patch_path.is_file() or _sha256_file(patch_path) != row["patch_sha256"]:
            self._fail(
                job_id,
                "PATCH_FILE_TAMPERED: stale apply reconciliation",
                expected=("applying",),
                guard_columns=apply_guard,
            )
            return
        forward = _git_run(
            repo, "apply", "--check", str(patch_path), check=False
        ).returncode == 0
        reverse = _git_run(
            repo, "apply", "--reverse", "--check", str(patch_path), check=False
        ).returncode == 0
        if reverse and not forward:
            if self._set_terminal(
                job_id,
                "integrated",
                error=None,
                expected=("applying",),
                guard_columns=apply_guard,
            ):
                self._remove_worktree(repo, Path(row["worktree"]))
        elif forward and not reverse:
            self._transition_status(
                job_id,
                "applying",
                "ready_to_apply",
                guard_columns=apply_guard,
                apply_token=None,
                applying_pid=None,
                applying_process_start=None,
            )
        else:
            self._set_terminal(
                job_id,
                "conflict",
                expected=("applying",),
                guard_columns=apply_guard,
                error=(
                    "APPLY_RECOVERY_AMBIGUOUS: patch is neither exclusively "
                    "forward- nor reverse-applicable"
                ),
            )

    def apply(self, job_id: str) -> "dict[str, Any]":
        """Fingerprint-check, ``git apply --check``, then apply the patch."""
        with contextlib.closing(self._state_conn()) as conn:
            row = self._get_job_row(conn, job_id)
        if row["status"] != "ready_to_apply":
            raise OrchestrationError(
                f"job {job_id} is not ready_to_apply (status={row['status']})"
            )
        apply_pid = os.getpid()
        apply_started = _process_start_time(apply_pid)
        apply_token = secrets.token_urlsafe(24)
        if not self._transition_status(
            job_id,
            "ready_to_apply",
            "applying",
            apply_token=apply_token,
            applying_pid=apply_pid,
            applying_process_start=apply_started,
        ):
            raise OrchestrationError(
                f"job {job_id} integration was claimed by another process"
            )
        with contextlib.closing(self._state_conn()) as conn:
            row = self._get_job_row(conn, job_id)
        apply_guard = {
            "apply_token": apply_token,
            "applying_pid": apply_pid,
            "applying_process_start": apply_started,
        }
        repo = Path(row["repo"])
        spec = json.loads(row["spec_json"])
        patch_path = Path(row["patch_path"])
        if not patch_path.is_file() or _sha256_file(patch_path) != row["patch_sha256"]:
            self._set_terminal(
                job_id,
                "failed",
                error="PATCH_FILE_TAMPERED: refusing to apply",
                expected=("applying",),
                guard_columns=apply_guard,
            )
            raise OrchestrationError(
                f"patch for job {job_id} is missing or tampered: {patch_path}"
            )

        current_head = _git(repo, "rev-parse", "HEAD")
        if current_head != row["base_commit"]:
            self._set_terminal(
                job_id,
                "conflict",
                expected=("applying",),
                guard_columns=apply_guard,
                error=f"MAIN_HEAD_CHANGED: {current_head} != {row['base_commit']}",
            )
            raise OwnershipConflict(
                "main checkout HEAD changed since dispatch: "
                f"{current_head} != {row['base_commit']}"
            )

        main_changed = _changed_paths(repo)
        unowned_main = sorted(
            {
                path
                for path in main_changed
                if not _is_owned(path, list(spec["clara_files"]))
            }
        )
        if unowned_main:
            self._set_terminal(
                job_id,
                "conflict",
                expected=("applying",),
                guard_columns=apply_guard,
                error="MAIN_SCOPE_VIOLATION: " + ", ".join(unowned_main),
            )
            raise OwnershipConflict(
                "main checkout changes outside the declared Clara lane: "
                + ", ".join(unowned_main)
            )
        unsafe_main = _changed_symlink_paths(repo, main_changed)
        if unsafe_main:
            self._set_terminal(
                job_id,
                "conflict",
                expected=("applying",),
                guard_columns=apply_guard,
                error="MAIN_SYMLINK_VIOLATION: " + ", ".join(unsafe_main),
            )
            raise OwnershipConflict(
                "main checkout changed symlink path(s): " + ", ".join(unsafe_main)
            )

        stored = json.loads(row["fingerprints_json"])
        current = _fingerprint_owned(repo, list(spec["clse_files"]))
        mismatched = sorted(
            path
            for path in set(stored) | set(current)
            if stored.get(path) != current.get(path)
        )
        if mismatched:
            self._set_terminal(
                job_id,
                "conflict",
                expected=("applying",),
                guard_columns=apply_guard,
                error="MAIN_OWNED_FILE_CHANGED: " + ", ".join(mismatched),
            )
            raise OwnershipConflict(
                "Clive-owned path(s) changed in the main checkout since "
                "dispatch: " + ", ".join(mismatched)
            )

        check = _git_run(repo, "apply", "--check", str(patch_path), check=False)
        if check.returncode != 0:
            stderr = check.stderr.decode("utf-8", "replace").strip()
            self._set_terminal(
                job_id,
                "conflict",
                error=f"APPLY_CHECK_FAILED: {stderr}",
                expected=("applying",),
                guard_columns=apply_guard,
            )
            raise OwnershipConflict(
                f"git apply --check failed for job {job_id}: {stderr}"
            )
        _git(repo, "apply", str(patch_path))
        if not self._set_terminal(
            job_id,
            "integrated",
            error=None,
            expected=("applying",),
            guard_columns=apply_guard,
        ):
            raise OrchestrationError(
                f"job {job_id} lost apply ownership before integration commit"
            )
        self._remove_worktree(repo, Path(row["worktree"]))
        return self.status(job_id)

    # -- cancel / cleanup ---------------------------------------------------

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> "dict[str, Any]":
        """Block this job's cards, reclaim only its PIDs, release ownership."""
        while True:
            with contextlib.closing(self._state_conn()) as conn:
                row = self._get_job_row(conn, job_id)
            current = row["status"]
            if current in TERMINAL_STATUSES:
                raise OrchestrationError(
                    f"job {job_id} already terminal (status={current})"
                )
            if current == "applying":
                raise OrchestrationError(
                    f"job {job_id} is applying; cancellation cannot race integration"
                )
            if current == "cancelling":
                break
            if self._transition_status(
                job_id,
                current,
                "cancelling",
                error=f"CANCELLING: {reason}",
            ):
                break
        # Re-read after winning cancellation so a concurrently published task ID
        # is included. Other pipeline writers can no longer leave cancelling.
        with contextlib.closing(self._state_conn()) as conn:
            row = self._get_job_row(conn, job_id)
        with contextlib.closing(kb.connect(board=self.board)) as conn:
            task_ids = {
                task_id
                for task_id in (
                row["implementation_task_id"],
                row["review_task_id"],
                )
                if task_id
            }
            # A process can die after idempotently creating either card but
            # before publishing its ID. Discover that crash-window card by the
            # immutable job marker, independent of assignee/profile spelling.
            task_ids.update(
                task.id
                for task in kb.list_tasks(conn)
                if job_id in task.title
            )
            for task_id in sorted(task_ids):
                task = kb.get_task(conn, task_id)
                if task is None:
                    continue
                run = kb.latest_run(conn, task_id)
                worker_pid = (
                    run.worker_pid if run and run.worker_pid else task.worker_pid
                )
                if task.status not in ("done", "archived"):
                    if not kb.cancel_task(conn, task_id, reason=reason):
                        raise OrchestrationError(
                            f"CANCEL_PENDING: Kanban task {task_id} changed concurrently"
                        )
                    task = kb.get_task(conn, task_id)
                    run = kb.latest_run(conn, task_id)
                    worker_pid = (
                        run.worker_pid if run and run.worker_pid
                        else (task.worker_pid if task else None)
                    )
                elif _process_group_alive(worker_pid):
                    _terminate_process_group(worker_pid, expected_task_id=task_id)
                if _process_group_alive(worker_pid):
                    raise OrchestrationError(
                        f"CANCEL_PENDING: worker process group for {task_id} "
                        "could not be identity-verified and stopped; ownership kept"
                    )
        # Do not signal the recorded supervisor PID: it may have been reused.
        # Marking the job terminal makes the detached loop exit on its next tick.
        if not self._set_terminal(
            job_id,
            "cancelled",
            error=f"CANCELLED: {reason}",
            expected=("cancelling",),
        ):
            raise OrchestrationError(
                f"CANCEL_PENDING: job {job_id} left cancelling state concurrently"
            )
        # Worktree is preserved for diagnosis; use cleanup() to remove it.
        return self.status(job_id)

    def cleanup(self, job_id: str) -> "dict[str, Any]":
        """Remove the disposable worktree of a terminal job (patch is kept)."""
        with contextlib.closing(self._state_conn()) as conn:
            row = self._get_job_row(conn, job_id)
        if row["status"] not in TERMINAL_STATUSES:
            raise OrchestrationError(
                f"cleanup requires a terminal job (status={row['status']})"
            )
        self._remove_worktree(Path(row["repo"]), Path(row["worktree"]))
        return self.status(job_id)

    def _remove_worktree(self, repo: Path, worktree: Path) -> None:
        """Best-effort removal of a generated worktree. Never raises."""
        if not str(worktree):
            return
        try:
            resolved_worktree = worktree.resolve(strict=False)
            resolved_root = self.worktrees_root.resolve(strict=True)
            if not resolved_worktree.is_relative_to(resolved_root):
                return
        except (OSError, RuntimeError):
            return
        with contextlib.suppress(Exception):
            _git_run(
                repo, "worktree", "remove", "--force", str(worktree), check=False
            )
        if worktree.is_dir():
            shutil.rmtree(worktree, ignore_errors=True)
        with contextlib.suppress(Exception):
            _git_run(repo, "worktree", "prune", check=False)

    # -- wait / supervise ---------------------------------------------------

    def wait(
        self,
        job_id: str,
        *,
        apply: bool = False,
        timeout_seconds: Optional[float] = None,
        poll_seconds: float = 2.0,
    ) -> "dict[str, Any]":
        """Poll until terminal (or ready_to_apply; with apply=True, integrate)."""
        deadline = (
            time.monotonic() + float(timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        while True:
            payload = self.tick(job_id)
            if payload["status"] == "ready_to_apply":
                if apply:
                    return self.apply(job_id)
                return payload
            if payload["status"] in TERMINAL_STATUSES:
                return payload
            if deadline is not None and time.monotonic() >= deadline:
                raise OrchestrationError(
                    f"wait timed out after {timeout_seconds}s "
                    f"(job {job_id} status={payload['status']})"
                )
            time.sleep(max(0.1, float(poll_seconds)))

    def supervise(
        self, job_id: str, *, poll_seconds: float = 5.0
    ) -> "dict[str, Any]":
        """Detached babysitter loop: tick, spawn workers, enforce timeout."""
        while True:
            payload = self.tick(job_id)
            if payload["status"] in TERMINAL_STATUSES or (
                payload["status"] == "ready_to_apply"
            ):
                # Applying is an explicit Clara decision — never automatic.
                return payload
            deadline = payload["created_at"] + payload["timeout_seconds"]
            if int(time.time()) >= deadline:
                return self.cancel(
                    job_id,
                    reason=f"timeout after {payload['timeout_seconds']}s",
                )
            if self.spawn_workers:
                self._try_spawn_workers()
            time.sleep(max(0.5, float(poll_seconds)))

    def _start_supervisor(self, job_id: str) -> Optional[int]:
        """Spawn the detached supervise loop for a job; record its PID."""
        log_path = self.logs_root / f"{job_id}.supervisor.log"
        env = {
            key: value
            for key, value in os.environ.items()
            if not any(key.startswith(p) for p in _SCRUB_ENV_PREFIXES)
        }
        argv = [
            sys.executable, "-m", "hermes_cli.clara_clive_orchestrator",
            "--runtime-root", str(self.runtime_root),
            "--board", self.board,
        ]
        if not self.spawn_workers:
            argv.append("--no-spawn-workers")
        argv += ["supervise", "--job-id", job_id]
        try:
            with open(log_path, "ab") as log_file:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    cwd=str(Path(__file__).resolve().parent.parent),
                    env=env,
                    start_new_session=True,
                )
        except OSError as exc:
            with contextlib.closing(self._state_conn()) as conn:
                with self._txn(conn):
                    self._update_job(
                        conn, job_id, error=f"SUPERVISOR_SPAWN_FAILED: {exc}"
                    )
            return None
        with contextlib.closing(self._state_conn()) as conn:
            with self._txn(conn):
                self._update_job(conn, job_id, supervisor_pid=proc.pid)
        return proc.pid


# Canonical public name after the Clse → Clive role rename. The original class
# remains available for source compatibility with existing imports.
ClaraCliveOrchestrator = ClaraClseOrchestrator


# ---------------------------------------------------------------------------
# JSON CLI
# ---------------------------------------------------------------------------

def _read_worker_file(path: Path, *, limit: int = 1024 * 1024) -> str:
    """Read a lifecycle payload only from the pinned Kanban workspace."""
    workspace_text = os.environ.get("HERMES_KANBAN_WORKSPACE")
    if not workspace_text:
        raise OrchestrationError("HERMES_KANBAN_WORKSPACE is required")
    workspace = Path(workspace_text).resolve(strict=True)
    lifecycle_root = workspace / ".clara-clive-lifecycle"
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise OrchestrationError(
            f"worker lifecycle file must be a regular non-symlink file: {candidate}"
        )
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(lifecycle_root):
        raise OrchestrationError(
            f"worker lifecycle file must stay inside {lifecycle_root}: {resolved}"
        )
    if resolved.stat().st_size > limit:
        raise OrchestrationError(f"worker lifecycle file exceeds {limit} bytes")
    return resolved.read_text(encoding="utf-8")


def _worker_lifecycle(args: argparse.Namespace) -> "dict[str, Any]":
    """Complete/block the current card using file inputs, never shell text."""
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    board = os.environ.get("HERMES_KANBAN_BOARD") or DEFAULT_BOARD
    run_text = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not task_id or not run_text:
        raise OrchestrationError(
            "worker lifecycle requires HERMES_KANBAN_TASK and HERMES_KANBAN_RUN_ID"
        )
    try:
        run_id = int(run_text)
    except ValueError as exc:
        raise OrchestrationError("invalid HERMES_KANBAN_RUN_ID") from exc
    claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
    worker_capability = os.environ.get("HERMES_KANBAN_WORKER_CAPABILITY")

    with contextlib.closing(kb.connect(board=board)) as conn:
        if args.command == "worker-complete":
            summary = _read_worker_file(args.summary_file, limit=256 * 1024).strip()
            metadata_text = _read_worker_file(args.metadata_file)
            try:
                metadata = json.loads(metadata_text)
            except json.JSONDecodeError as exc:
                raise OrchestrationError(f"invalid metadata JSON: {exc}") from exc
            if not summary or not isinstance(metadata, dict):
                raise OrchestrationError("summary and metadata object are required")
            ok = kb.complete_task(
                conn,
                task_id,
                summary=summary,
                metadata=metadata,
                expected_run_id=run_id,
                expected_claim_lock=claim_lock,
                expected_worker_capability=worker_capability,
            )
        else:
            reason = _read_worker_file(args.reason_file, limit=256 * 1024).strip()
            if not reason:
                raise OrchestrationError("block reason is required")
            ok = kb.block_task(
                conn,
                task_id,
                reason=reason,
                expected_run_id=run_id,
                expected_claim_lock=claim_lock,
                expected_worker_capability=worker_capability,
            )
    if not ok:
        raise OrchestrationError(
            f"lifecycle transition refused for task {task_id}, run {run_id}"
        )
    return {
        "board": board,
        "task_id": task_id,
        "run_id": run_id,
        "status": "done" if args.command == "worker-complete" else "blocked",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clara-clive",
        description="Clara→Clive parallel orchestration coordinator (JSON output).",
    )
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--no-spawn-workers", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    dispatch = sub.add_parser("dispatch", help="validate, lock, and dispatch")
    dispatch.add_argument("--repo", type=Path, required=True)
    dispatch.add_argument("--title", required=True)
    dispatch.add_argument("--task-file", type=Path, required=True)
    dispatch.add_argument(
        "--clive-file", "--clse-file", action="append", required=True,
        dest="clse_files", help="Clive-owned repo-relative path (legacy: --clse-file)",
    )
    dispatch.add_argument(
        "--clara-file", action="append", default=[], dest="clara_files"
    )
    dispatch.add_argument("--test", action="append", default=[], dest="tests")
    dispatch.add_argument(
        "--timeout-seconds", "--timeout", dest="timeout_seconds",
        type=int, default=DEFAULT_TIMEOUT_SECONDS,
    )
    dispatch.add_argument("--no-supervisor", action="store_true")

    worker_complete = sub.add_parser(
        "worker-complete", help="complete the current Kanban run from files"
    )
    worker_complete.add_argument("--summary-file", type=Path, required=True)
    worker_complete.add_argument("--metadata-file", type=Path, required=True)
    worker_block = sub.add_parser(
        "worker-block", help="block the current Kanban run from a reason file"
    )
    worker_block.add_argument("--reason-file", type=Path, required=True)

    for name in ("status", "cancel", "cleanup", "wait", "supervise"):
        cmd = sub.add_parser(name)
        cmd.add_argument("job_id_pos", nargs="?")
        cmd.add_argument("--job-id", dest="job_id_opt")
    sub.choices["cancel"].add_argument("--reason", default="cancelled via CLI")
    sub.choices["wait"].add_argument("--apply", action="store_true")
    sub.choices["wait"].add_argument(
        "--timeout-seconds", "--timeout", dest="timeout_seconds",
        type=float, default=None,
    )
    sub.choices["wait"].add_argument("--poll-seconds", type=float, default=2.0)
    sub.choices["supervise"].add_argument(
        "--poll-seconds", type=float, default=5.0
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    orchestrator = ClaraClseOrchestrator(
        runtime_root=args.runtime_root,
        board=args.board,
        spawn_workers=not args.no_spawn_workers,
    )
    try:
        if args.command in ("worker-complete", "worker-block"):
            payload = _worker_lifecycle(args)
        elif args.command == "dispatch":
            task_path = args.task_file.expanduser()
            if task_path.is_symlink() or not task_path.is_file():
                raise OrchestrationError(
                    f"task file must be a regular non-symlink file: {task_path}"
                )
            if task_path.stat().st_size > 1024 * 1024:
                raise OrchestrationError("task file exceeds 1 MiB")
            task_text = task_path.read_text(encoding="utf-8")
            spec = JobSpec(
                repo=args.repo,
                title=args.title,
                task=task_text,
                clse_files=tuple(args.clse_files),
                clara_files=tuple(args.clara_files),
                tests=tuple(args.tests),
                timeout_seconds=args.timeout_seconds,
            )
            payload = orchestrator.dispatch(
                spec, start_supervisor=not args.no_supervisor
            )
        else:
            job_id = args.job_id_opt or args.job_id_pos
            if not job_id:
                raise OrchestrationError(
                    f"{args.command} requires a job id (positional or --job-id)"
                )
            if args.command == "status":
                payload = orchestrator.status(job_id)
            elif args.command == "wait":
                payload = orchestrator.wait(
                    job_id,
                    apply=args.apply,
                    timeout_seconds=args.timeout_seconds,
                    poll_seconds=args.poll_seconds,
                )
            elif args.command == "cancel":
                payload = orchestrator.cancel(job_id, reason=args.reason)
            elif args.command == "cleanup":
                payload = orchestrator.cleanup(job_id)
            elif args.command == "supervise":
                payload = orchestrator.supervise(
                    job_id, poll_seconds=args.poll_seconds
                )
            else:  # pragma: no cover - argparse enforces the choices
                raise OrchestrationError(f"unknown command: {args.command}")
    except OwnershipConflict as exc:
        print(json.dumps({"error": str(exc), "error_type": "OwnershipConflict"}))
        return 3
    except OrchestrationError as exc:
        print(json.dumps({"error": str(exc), "error_type": "OrchestrationError"}))
        return 2
    print(json.dumps(payload, sort_keys=True))
    if (
        args.command == "wait"
        and isinstance(payload, dict)
        and payload.get("status") in {"failed", "cancelled", "conflict"}
    ):
        return 4
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
