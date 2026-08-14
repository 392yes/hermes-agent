"""Launcher-scoped live status for the classic ``hermes-loadout`` TUI.

This state is presentation-only.  It never changes conversation history,
approval state, or the separate ``scripts/orchestrate.py`` canonical run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any


STATUS_META: dict[str, tuple[str, str]] = {
    "RUNNING": ("🟢", "실행 중"),
    "WAITING": ("🔵", "모델/도구/네트워크 응답 대기"),
    "DELAYED": ("🟡", "응답 지연"),
    "STALLED": ("🟠", "진행 이벤트 장기 부재"),
    "DISCONNECTED": ("🔴", "worker 연결 끊김"),
    "WAITING APPROVAL": ("🟣", "사용자 승인 대기"),
    "COMPLETED": ("✅", "완료"),
    "FAILED": ("❌", "오류 종료"),
    "STOPPED": ("⛔", "사용자 중단"),
}

_ACTIVE_STATUSES = {"RUNNING", "WAITING", "DELAYED", "STALLED"}
_CALLBACK_STATUSES = {"RUNNING", "WAITING", "WAITING APPROVAL"}
_TERMINAL_STATUSES = {"DISCONNECTED", "COMPLETED", "FAILED", "STOPPED"}


@dataclass
class LoadoutTurnStatus:
    """Thread-safe state machine for one interactive Hermes turn."""

    delayed_after: float = 60.0
    stalled_after: float = 300.0
    status: str | None = None
    phase: str = ""
    detail: str = ""
    turn_started_at: float | None = None
    last_progress_at: float | None = None
    updated_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else float(now)

    def _set(
        self,
        status: str,
        *,
        now: float | None = None,
        phase: str = "",
        detail: str = "",
        progress: bool = True,
    ) -> None:
        if status not in STATUS_META:
            raise ValueError(f"unknown loadout turn status: {status}")
        timestamp = self._now(now)
        with self._lock:
            if self.status in _TERMINAL_STATUSES and status in _CALLBACK_STATUSES:
                return
            self.status = status
            if phase:
                self.phase = str(phase)
            if detail:
                self.detail = str(detail)
            self.updated_at = timestamp
            if progress:
                self.last_progress_at = timestamp

    def start(self, *, now: float | None = None, phase: str = "Hermes 요청 처리") -> None:
        timestamp = self._now(now)
        with self._lock:
            self.status = "RUNNING"
            self.phase = phase
            self.detail = STATUS_META["RUNNING"][1]
            self.turn_started_at = timestamp
            self.last_progress_at = timestamp
            self.updated_at = timestamp

    def progress(self, *, now: float | None = None, phase: str = "tool 실행") -> None:
        self._set("RUNNING", now=now, phase=phase, detail=STATUS_META["RUNNING"][1])

    def wait(self, *, now: float | None = None, phase: str = "모델 응답") -> None:
        self._set("WAITING", now=now, phase=phase, detail=STATUS_META["WAITING"][1])

    def wait_for_approval(self, *, now: float | None = None) -> None:
        self._set(
            "WAITING APPROVAL",
            now=now,
            phase="승인",
            detail=STATUS_META["WAITING APPROVAL"][1],
        )

    def disconnect(self, *, now: float | None = None) -> None:
        self._set("DISCONNECTED", now=now, detail=STATUS_META["DISCONNECTED"][1])

    def complete(self, *, now: float | None = None) -> None:
        self._set("COMPLETED", now=now, detail=STATUS_META["COMPLETED"][1])

    def fail(self, *, now: float | None = None) -> None:
        self._set("FAILED", now=now, detail=STATUS_META["FAILED"][1])

    def stop(self, *, now: float | None = None) -> None:
        self._set("STOPPED", now=now, detail=STATUS_META["STOPPED"][1])

    def snapshot(self, *, now: float | None = None, worker_alive: bool | None = None) -> dict[str, Any]:
        timestamp = self._now(now)
        with self._lock:
            status = self.status
            phase = self.phase
            detail = self.detail
            started_at = self.turn_started_at
            last_progress_at = self.last_progress_at
            updated_at = self.updated_at

        if status is None:
            return {
                "status": None,
                "emoji": "",
                "label": "",
                "phase": "",
                "detail": "",
                "elapsed_seconds": 0,
                "progress_age_seconds": None,
            }

        if worker_alive is False and status in _ACTIVE_STATUSES:
            status = "DISCONNECTED"
            detail = STATUS_META[status][1]
        elif status in _ACTIVE_STATUSES and last_progress_at is not None:
            age = max(0.0, timestamp - last_progress_at)
            if age >= self.stalled_after:
                status = "STALLED"
                detail = STATUS_META[status][1]
            elif age >= self.delayed_after:
                status = "DELAYED"
                detail = STATUS_META[status][1]

        emoji, default_detail = STATUS_META[status]
        elapsed = max(0.0, timestamp - started_at) if started_at is not None else 0.0
        progress_age = (
            max(0.0, timestamp - last_progress_at)
            if last_progress_at is not None
            else None
        )
        return {
            "status": status,
            "emoji": emoji,
            "label": f"{emoji} {status}",
            "phase": phase,
            "detail": detail or default_detail,
            "elapsed_seconds": int(elapsed),
            "progress_age_seconds": int(progress_age) if progress_age is not None else None,
            "updated_at": updated_at,
        }
