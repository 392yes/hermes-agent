"""Read-only canonical status bridge for background ``hermes-loadout`` runs.

The monitor performs process discovery and ``orchestrate.py status --json``
probes on its own daemon thread.  Prompt-toolkit render paths only read the
cached snapshot, so a slow status probe cannot block typing or repainting.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from hermes_cli.loadout_turn_status import STATUS_META


_ALLOWED_SUBCOMMANDS = frozenset({"start", "resume", "approve"})
_CANONICAL_STATUSES = frozenset(STATUS_META)
_ACTIVE_CANONICAL_STATUSES = frozenset(
    {
        "RUNNING",
        "WAITING",
        "DELAYED",
        "STALLED",
        "DISCONNECTED",
        "WAITING APPROVAL",
    }
)
_TERMINAL_CANONICAL_STATUSES = frozenset({"COMPLETED", "FAILED", "STOPPED"})
_PYTHON_NAME_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$", re.IGNORECASE)
_MAX_STATUS_OUTPUT_BYTES = 256 * 1024
_VALUE_OPTIONS = {
    "start": frozenset(
        {
            "--target",
            "--task-file",
            "--mode",
            "--execution-mode",
            "--approval-target",
            "--hermes-bin",
            "--store-script",
            "--timeout",
            "--approval-mode",
        }
    ),
    "resume": frozenset({"--target"}),
    "approve": frozenset({"--target", "--approval-id", "--option"}),
}
_FLAG_OPTIONS = {
    "start": frozenset({"--dry-run", "--notify-slack", "--no-notify-slack"}),
    "resume": frozenset(),
    "approve": frozenset(),
}


@dataclass(frozen=True)
class LoadoutRunCandidate:
    """A validated process-registry entry for one canonical Loadout run."""

    session_id: str
    session_key: str
    task_id: str
    script_path: Path
    target: Path
    subcommand: str
    started_at: float
    pid: int | None
    running: bool


def _under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _command_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(
        command,
        posix=True,
        punctuation_chars=";&|<>",
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _target_from_options(tokens: Sequence[str], subcommand: str) -> str | None:
    """Parse the known orchestrator option grammar and return one target."""

    value_options = _VALUE_OPTIONS[subcommand]
    flag_options = _FLAG_OPTIONS[subcommand]
    targets: list[str] = []
    index = 3
    while index < len(tokens):
        token = tokens[index]
        if token and all(char in ";&|<>" for char in token):
            return None
        if token in flag_options:
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            option, value = token.split("=", 1)
            if option not in value_options or not value:
                return None
            if option == "--target":
                targets.append(value)
            index += 1
            continue
        if token not in value_options or index + 1 >= len(tokens):
            return None
        value = tokens[index + 1]
        if not value or value.startswith("--"):
            return None
        if token == "--target":
            targets.append(value)
        index += 2
    return targets[0] if len(targets) == 1 else None


def parse_loadout_process(
    entry: Mapping[str, Any],
    *,
    session_key: str,
    skill_roots: Sequence[Path],
) -> LoadoutRunCandidate | None:
    """Validate and bind a registry entry without executing its command."""

    if not isinstance(entry, Mapping):
        return None
    if str(entry.get("session_key") or "") != str(session_key or ""):
        return None

    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if "$" in command or "`" in command:
        return None
    try:
        tokens = _command_tokens(command)
    except (TypeError, ValueError):
        return None
    if len(tokens) < 5:
        return None
    if not _PYTHON_NAME_RE.fullmatch(Path(tokens[0]).name):
        return None

    script_raw = Path(tokens[1]).expanduser()
    if not script_raw.is_absolute():
        cwd = Path(str(entry.get("cwd") or ".")).expanduser()
        script_raw = cwd / script_raw
    try:
        script_path = script_raw.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if script_raw.is_symlink() or not script_path.is_file():
        return None
    skill_dir = script_path.parent.parent
    if (
        script_path.name != "orchestrate.py"
        or script_path.parent.name != "scripts"
        or skill_dir.name != "hermes-loadout"
        or not (skill_dir / "SKILL.md").is_file()
    ):
        return None

    resolved_roots: list[Path] = []
    for root in skill_roots:
        try:
            resolved_roots.append(Path(root).expanduser().resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    if not any(_under_root(script_path, root) for root in resolved_roots):
        return None

    subcommand = tokens[2]
    if subcommand not in _ALLOWED_SUBCOMMANDS:
        return None

    target_value = _target_from_options(tokens, subcommand)
    if target_value is None:
        return None

    target_raw = Path(target_value).expanduser()
    if not target_raw.is_absolute():
        target_raw = Path(str(entry.get("cwd") or ".")).expanduser() / target_raw
    try:
        target = target_raw.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if target_raw.is_symlink() or not target.is_dir():
        return None

    try:
        started_at = float(entry.get("started_at") or 0.0)
    except (TypeError, ValueError):
        started_at = 0.0
    try:
        pid_value = entry.get("pid")
        pid = int(pid_value) if pid_value is not None else None
    except (TypeError, ValueError):
        pid = None

    return LoadoutRunCandidate(
        session_id=str(entry.get("session_id") or ""),
        session_key=str(entry.get("session_key") or ""),
        task_id=str(entry.get("task_id") or ""),
        script_path=script_path,
        target=target,
        subcommand=subcommand,
        started_at=started_at,
        pid=pid,
        running=str(entry.get("status") or "") == "running",
    )


def _single_line(value: Any, *, limit: int = 48) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def normalize_status_report(
    report: Mapping[str, Any],
    candidate: LoadoutRunCandidate,
) -> dict[str, Any]:
    """Validate canonical JSON and build the one-line footer snapshot."""

    if not isinstance(report, Mapping):
        raise ValueError("loadout status report must be an object")
    try:
        report_target = Path(str(report.get("target") or "")).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("loadout status target is invalid") from None
    if report_target != candidate.target:
        raise ValueError("loadout status target does not match the bound process")

    runtime = report.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("loadout runtime status is missing")
    status = str(runtime.get("display_status") or "").strip().upper()
    if status not in _CANONICAL_STATUSES:
        raise ValueError(f"unknown canonical loadout status: {status or '<empty>'}")
    run_id = _single_line(report.get("run_id"), limit=96)
    if not run_id:
        raise ValueError("loadout run id is missing")

    emoji = STATUS_META[status][0]
    phase_label = _single_line(runtime.get("phase_label"), limit=32)
    cycle_display = _single_line(runtime.get("cycle_display"), limit=16)
    heartbeat_source = _single_line(runtime.get("heartbeat_source"), limit=32)
    heartbeat_age = runtime.get("heartbeat_age_seconds")
    try:
        heartbeat_age_int = max(0, int(heartbeat_age)) if heartbeat_age is not None else None
    except (TypeError, ValueError):
        heartbeat_age_int = None

    parts = [f"{emoji} LOADOUT {status}"]
    if phase_label:
        parts.append(phase_label)
    if status not in _TERMINAL_CANONICAL_STATUSES:
        if heartbeat_source == "runtime" and heartbeat_age_int is not None:
            parts.append(f"heartbeat {heartbeat_age_int}초 전")
        elif heartbeat_source == "process-observation":
            parts.append("heartbeat 프로세스 관찰")
        else:
            parts.append("heartbeat 확인 불가")
    if cycle_display:
        parts.append(cycle_display)

    return {
        "source": "orchestrator",
        "status": status,
        "emoji": emoji,
        "label": " · ".join(parts),
        "phase": _single_line(runtime.get("phase"), limit=48),
        "phase_label": phase_label,
        "detail": _single_line(
            (runtime.get("progress") or {}).get("detail")
            if isinstance(runtime.get("progress"), Mapping)
            else "",
            limit=96,
        ),
        "target": str(candidate.target),
        "run_id": run_id,
        "process_session_id": candidate.session_id,
        "parent_pid": runtime.get("parent_pid"),
        "cycle_display": cycle_display,
        "heartbeat_age_seconds": heartbeat_age_int,
        "heartbeat_source": heartbeat_source,
        "terminal": status in _TERMINAL_CANONICAL_STATUSES,
    }


def _default_skill_roots() -> list[Path]:
    from agent.skill_utils import get_all_skills_dirs

    return [Path(path) for path in get_all_skills_dirs()]


def _default_session_source(session_key: str) -> list[dict[str, Any]]:
    from tools.process_registry import process_registry

    return process_registry.snapshot_sessions(
        session_key=session_key,
        include_finished=True,
    )


def _default_probe(candidate: LoadoutRunCandidate, timeout: float) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(candidate.script_path),
            "status",
            "--target",
            str(candidate.target),
            "--json",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"loadout status exited {completed.returncode}")
    encoded = completed.stdout.encode("utf-8", errors="replace")
    if len(encoded) > _MAX_STATUS_OUTPUT_BYTES:
        raise RuntimeError("loadout status output exceeded the size limit")
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        raise ValueError("loadout status output must be a JSON object")
    return parsed


class LoadoutOrchestratorStatusMonitor:
    """Poll canonical Loadout status off-thread and expose a cached snapshot."""

    def __init__(
        self,
        *,
        session_key: str,
        session_source: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
        skill_roots: Sequence[Path] | None = None,
        probe: Callable[[LoadoutRunCandidate], Mapping[str, Any]] | None = None,
        on_change: Callable[[], None] | None = None,
        interval: float = 3.0,
        probe_timeout: float = 2.5,
        stale_after: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_key = str(session_key or "")
        self._session_source = session_source or (
            lambda: _default_session_source(self.session_key)
        )
        self._skill_roots = list(skill_roots) if skill_roots is not None else None
        self._probe = probe
        self._on_change = on_change
        self.interval = max(0.2, float(interval))
        self.probe_timeout = max(0.2, float(probe_timeout))
        self.stale_after = max(self.interval, float(stale_after))
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._bound: LoadoutRunCandidate | None = None
        self._last_success_at: float | None = None
        self._generation = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _roots(self) -> Sequence[Path]:
        if self._skill_roots is None:
            self._skill_roots = _default_skill_roots()
        return self._skill_roots

    def _candidates(self) -> tuple[int, list[LoadoutRunCandidate]]:
        result: list[LoadoutRunCandidate] = []
        with self._lock:
            session_key = self.session_key
            generation = self._generation
        for entry in self._session_source():
            candidate = parse_loadout_process(
                entry,
                session_key=session_key,
                skill_roots=self._roots(),
            )
            if candidate is not None:
                result.append(candidate)
        return generation, result

    def _choose_candidate(
        self,
        candidates: Sequence[LoadoutRunCandidate],
        *,
        expected_generation: int,
    ) -> LoadoutRunCandidate | None:
        running = [candidate for candidate in candidates if candidate.running]
        if running:
            running.sort(key=lambda candidate: candidate.started_at, reverse=True)
            targets = {candidate.target for candidate in running}
            if len(targets) > 1:
                self._replace_snapshot(
                    {
                        "source": "orchestrator",
                        "status": "DELAYED",
                        "emoji": "🟡",
                        "label": "🟡 LOADOUT DELAYED · 여러 실행 감지",
                        "phase": "",
                        "phase_label": "",
                        "detail": "대상 선택 불가",
                        "terminal": False,
                    },
                    expected_generation=expected_generation,
                )
                return None
            newest = running[0]
            with self._lock:
                if expected_generation != self._generation:
                    return None
                current = self._bound
                is_new = current is None or (
                    newest.session_id != current.session_id
                    and newest.started_at >= current.started_at
                )
                if is_new:
                    self._bound = newest
                    self._last_success_at = None
                    self._generation += 1
                selected = self._bound
                generation = self._generation
            if is_new:
                self._replace_snapshot(
                    {
                        "source": "orchestrator",
                        "status": "DELAYED",
                        "emoji": "🟡",
                        "label": "🟡 LOADOUT DELAYED · 상태 확인 중",
                        "phase": "",
                        "phase_label": "",
                        "detail": "canonical status probe pending",
                        "process_session_id": newest.session_id,
                        "target": str(newest.target),
                        "terminal": False,
                    },
                    expected_generation=generation,
                )
            return selected
        with self._lock:
            if expected_generation != self._generation:
                return None
            return self._bound

    def _replace_snapshot(
        self,
        snapshot: dict[str, Any] | None,
        *,
        expected_generation: int | None = None,
    ) -> None:
        callback = None
        with self._lock:
            if (
                expected_generation is not None
                and expected_generation != self._generation
            ):
                return
            if snapshot != self._snapshot:
                self._snapshot = dict(snapshot) if snapshot is not None else None
                callback = self._on_change
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def _mark_probe_stale(self, now: float) -> None:
        with self._lock:
            current = dict(self._snapshot) if self._snapshot is not None else None
            last_success = self._last_success_at
        if (
            current is None
            or current.get("status") in _TERMINAL_CANONICAL_STATUSES
            or last_success is None
            or now - last_success < self.stale_after
        ):
            return
        current.update(
            {
                "status": "DISCONNECTED",
                "emoji": "🔴",
                "label": "🔴 LOADOUT DISCONNECTED · 상태 확인 지연",
                "detail": "canonical status probe stale",
                "terminal": False,
            }
        )
        self._replace_snapshot(current)

    def rebind_session(self, session_key: str) -> None:
        """Clear cached run state after the interactive CLI rotates sessions."""
        callback = None
        with self._lock:
            new_key = str(session_key or "")
            if new_key == self.session_key:
                return
            self.session_key = new_key
            self._bound = None
            self._last_success_at = None
            self._generation += 1
            if self._snapshot is not None:
                self._snapshot = None
                callback = self._on_change
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def refresh_once(self) -> None:
        """Perform one discovery/probe cycle; intended for the monitor thread."""

        now = self._clock()
        try:
            discovery_generation, candidates = self._candidates()
            candidate = self._choose_candidate(
                candidates,
                expected_generation=discovery_generation,
            )
            if candidate is None:
                self._mark_probe_stale(now)
                return
            with self._lock:
                generation = self._generation
            if self._probe is None:
                report = _default_probe(candidate, self.probe_timeout)
            else:
                report = self._probe(candidate)
            snapshot = normalize_status_report(report, candidate)
        except Exception:
            self._mark_probe_stale(now)
            return

        with self._lock:
            if (
                generation != self._generation
                or self._bound is None
                or self._bound.session_id != candidate.session_id
            ):
                return
            self._last_success_at = now
        self._replace_snapshot(snapshot, expected_generation=generation)

    def snapshot(self) -> dict[str, Any] | None:
        """Return cached presentation data without filesystem or process I/O."""

        with self._lock:
            return dict(self._snapshot) if self._snapshot is not None else None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.refresh_once()
            self._stop_event.wait(self.interval)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="loadout-orchestrator-status",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.probe_timeout + 0.5)
        self._thread = None


__all__ = [
    "LoadoutOrchestratorStatusMonitor",
    "LoadoutRunCandidate",
    "normalize_status_report",
    "parse_loadout_process",
]
