"""Process-scoped automatic dispatcher for the dedicated hermes-loadout launcher."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any
import uuid

AUTO_LOADOUT_ENV = "HERMES_LOADOUT_AUTO_ORCHESTRATE"
AUTO_LOADOUT_SYSTEM_PROMPT = """[Automatic Loadout execution mode is active.]
For every substantive non-control user request, immediately execute the preloaded hermes-loadout skill contract by starting `scripts/orchestrate.py start` with the user's exact task. Do not implement the task directly in the foreground Hermes turn. Do not require /hermes-loadout; ordinary task text is the invocation. Resolve fast versus strict, parent tests, Coda/Hugo gates, multipart review, immutable fingerprints, timeout/turn budgets, and critical-only approvals through the canonical runner. Built-in TUI controls, canonical status/resume/approve/reject operations, approval replies, and status-only questions control the current run instead of starting a competing run. Never overlap a second writer on the same target."""

_MAX_TASK_BYTES = 128 * 1024
_APPROVAL_RE = re.compile(
    r"^승인\s+([A-Za-z0-9_-]{1,128})\s+선택\s+([A-Za-z0-9_-]{1,128})$"
)
_REJECT_RE = re.compile(r"^거절\s+([A-Za-z0-9_-]{1,128})$")
_STATUS_INPUTS = frozenset(
    {
        "status",
        "status check",
        "상태",
        "상태 확인",
        "현재 상태",
        "현재 상태 확인",
        "진행 상황",
        "진행상황",
        "어디까지",
        "어디까지 했어",
    }
)
_STATUS_RE = re.compile(
    r"^(?:(?:현재\s+)?(?:작업\s+)?(?:상태|진행\s*상황)"
    r"(?:\s+(?:확인|확인해줘|알려줘|보여줘))?|어디까지(?:\s+진행됐어)?)$"
)
_RESUME_INPUTS = frozenset(
    {
        "continue",
        "resume",
        "계속",
        "계속 진행",
        "계속해",
        "재개",
        "재개해",
    }
)
_INTERNAL_PREFIXES = (
    "[IMPORTANT: Background process ",
    "[IMPORTANT: The user has invoked the ",
    "[IMPORTANT: The user has invoked the following skill bundle,",
)


class AutoLoadoutDispatchError(RuntimeError):
    """A launcher-scoped automatic dispatch failed closed."""


@dataclass(frozen=True)
class AutoLoadoutDispatchResult:
    handled: bool
    action: str
    message: str = ""
    session_id: str = ""
    target: str = ""


def apply_automatic_loadout_contract(
    system_prompt: str,
    loaded_skills: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Append the automatic runner contract only for the dedicated launcher."""

    env = os.environ if environ is None else environ
    if env.get(AUTO_LOADOUT_ENV) != "1":
        return system_prompt
    if "hermes-loadout" not in loaded_skills:
        raise ValueError(
            "automatic hermes-loadout mode requires the hermes-loadout skill"
        )
    if AUTO_LOADOUT_SYSTEM_PROMPT in system_prompt:
        return system_prompt
    return "\n\n".join(
        part for part in (system_prompt, AUTO_LOADOUT_SYSTEM_PROMPT) if part
    ).strip()


def _classify_request(text: str) -> tuple[str, tuple[str, ...]]:
    stripped = text.strip()
    if not stripped or stripped.startswith(_INTERNAL_PREFIXES):
        return "passthrough", ()
    normalized = " ".join(stripped.lower().split()).rstrip("?？")
    if normalized in _STATUS_INPUTS or _STATUS_RE.fullmatch(normalized):
        return "status", ()
    if normalized in _RESUME_INPUTS:
        return "resume", ()
    approval = _APPROVAL_RE.fullmatch(stripped)
    if approval:
        return "approve", approval.groups()
    rejection = _REJECT_RE.fullmatch(stripped)
    if rejection:
        return "reject", rejection.groups()
    if re.match(r"^수정\s+[A-Za-z0-9_-]{1,128}\s*:", stripped):
        return "passthrough", ()
    return "start", ()


def _resolve_target(cwd: str | os.PathLike[str] | None) -> Path:
    raw = Path(cwd or os.environ.get("TERMINAL_CWD") or os.getcwd()).expanduser()
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AutoLoadoutDispatchError(f"loadout target is unavailable: {raw}") from exc
    if not resolved.is_dir():
        raise AutoLoadoutDispatchError(f"loadout target is not a directory: {resolved}")
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


def _resolve_orchestrator_path(session_key: str) -> Path:
    try:
        from tools.skills_tool import skill_view

        payload = json.loads(
            skill_view("hermes-loadout", task_id=session_key, preprocess=False)
        )
    except Exception as exc:
        raise AutoLoadoutDispatchError("could not resolve hermes-loadout skill") from exc
    skill_dir_raw = payload.get("skill_dir") if isinstance(payload, dict) else None
    if not skill_dir_raw:
        raise AutoLoadoutDispatchError("hermes-loadout skill directory is missing")
    script = Path(str(skill_dir_raw)) / "scripts" / "orchestrate.py"
    try:
        resolved = script.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AutoLoadoutDispatchError("hermes-loadout orchestrator is missing") from exc
    if script.is_symlink() or not resolved.is_file():
        raise AutoLoadoutDispatchError("hermes-loadout orchestrator must be a regular file")
    return resolved


def _runtime_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "runtime" / "loadout-auto-tasks"


def _write_task_file(task: str, runtime_dir: Path) -> Path:
    payload = task.encode("utf-8")
    if not payload or len(payload) > _MAX_TASK_BYTES:
        raise AutoLoadoutDispatchError("loadout task must be 1..131072 UTF-8 bytes")
    if runtime_dir.exists() and runtime_dir.is_symlink():
        raise AutoLoadoutDispatchError("loadout task directory must not be a symlink")
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(runtime_dir, 0o700)
    except OSError:
        pass
    task_path = runtime_dir / f"task-{uuid.uuid4().hex}.txt"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(task_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            task_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    file_stat = task_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(file_stat.st_mode):
        task_path.unlink(missing_ok=True)
        raise AutoLoadoutDispatchError("loadout task file must be regular")
    return task_path


def _default_spawn_background(
    argv: Sequence[str],
    *,
    cwd: Path,
    session_key: str,
    env_overrides: Mapping[str, str],
) -> str:
    from tools.process_registry import process_registry

    session = process_registry.spawn_local(
        command=shlex.join(str(part) for part in argv),
        cwd=str(cwd),
        task_id=session_key,
        session_key=session_key,
        env_vars=dict(env_overrides),
        use_pty=False,
    )
    session.notify_on_complete = True
    return session.id


def _default_run_control(
    argv: Sequence[str],
    *,
    cwd: Path,
    env_overrides: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [str(part) for part in argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        shell=False,
    )


def dispatch_automatic_loadout_request(
    task: str,
    *,
    loaded_skills: Sequence[str],
    session_key: str,
    cwd: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    active_run: bool = False,
    orchestrator_path: Path | None = None,
    runtime_dir: Path | None = None,
    spawn_background: Callable[..., str] | None = None,
    run_control: Callable[..., Any] | None = None,
) -> AutoLoadoutDispatchResult:
    """Deterministically route one launcher task before the foreground model."""

    env = os.environ if environ is None else environ
    if env.get(AUTO_LOADOUT_ENV) != "1":
        return AutoLoadoutDispatchResult(False, "disabled")
    if "hermes-loadout" not in loaded_skills:
        raise AutoLoadoutDispatchError(
            "automatic hermes-loadout mode requires the hermes-loadout skill"
        )
    action, control_args = _classify_request(task)
    if action == "passthrough":
        return AutoLoadoutDispatchResult(False, action)
    target = _resolve_target(cwd)
    script = (
        Path(orchestrator_path).resolve(strict=True)
        if orchestrator_path is not None
        else _resolve_orchestrator_path(session_key)
    )
    if script.is_symlink() or not script.is_file():
        raise AutoLoadoutDispatchError("hermes-loadout orchestrator must be a regular file")
    if action != "status" and active_run:
        return AutoLoadoutDispatchResult(
            True,
            "active",
            "A canonical hermes-loadout run is already active for this session; no competing writer was started.",
            target=str(target),
        )

    argv = [sys.executable, str(script), action, "--target", str(target)]
    if action == "start":
        task_file = _write_task_file(task, Path(runtime_dir or _runtime_dir()))
        argv.extend(
            [
                "--task-file",
                str(task_file),
                "--execution-mode",
                "fast",
                "--approval-target",
                "slack:C0B49801526",
                "--approval-mode",
                "critical-only",
            ]
        )
    elif action == "approve":
        argv.extend(["--approval-id", control_args[0], "--option", control_args[1]])
    elif action == "reject":
        argv.extend(["--approval-id", control_args[0]])

    env_overrides = {AUTO_LOADOUT_ENV: "0"}
    if action in {"start", "resume", "approve"}:
        spawn = spawn_background or _default_spawn_background
        session_id = str(
            spawn(
                argv,
                cwd=target,
                session_key=session_key,
                env_overrides=env_overrides,
            )
        )
        return AutoLoadoutDispatchResult(
            True,
            action,
            f"Automatic hermes-loadout {action} started: {session_id} · target={target}",
            session_id=session_id,
            target=str(target),
        )

    control = run_control or _default_run_control
    completed = control(argv, cwd=target, env_overrides=env_overrides)
    output = str(completed.stdout or "").strip()
    error = str(completed.stderr or "").strip()
    if int(completed.returncode) != 0:
        detail = error or output or f"exit {completed.returncode}"
        return AutoLoadoutDispatchResult(
            True,
            action,
            f"Automatic hermes-loadout {action} failed: {detail}",
            target=str(target),
        )
    return AutoLoadoutDispatchResult(
        True,
        action,
        output or f"Automatic hermes-loadout {action} completed.",
        target=str(target),
    )
