"""Resident (long-running) Claude Code CLI process pool for Clara turns.

The default Clara bridge (:mod:`gateway.claude_code_bridge`) spawns a fresh
``claude`` process for every turn and relies on ``--resume`` plus Anthropic's
~5 minute server-side prompt cache to stay "warm".  That cache expires on idle
gaps, so the next turn pays a full cold-start.

This module keeps one ``claude`` process alive per (bridge_session_key, workdir)
and drives it with ``--input-format stream-json --output-format stream-json``.
The conversation context then lives in the process's own memory, so warmth is
independent of the 5 minute TTL and the per-turn process boot cost disappears.

Safety/laziness design notes:
- The pool lives inside the already-resident gateway process; nothing here
  changes Hugo's native path (that path never reaches this module).
- Each session key is turn-serialized by its own lock so concurrent Slack
  messages on one thread can never interleave on a single process.
- Dead processes, protocol errors, and timeouts raise :class:`ResidentTurnError`
  so the bridge can respawn-and-retry once and then fall back to the classic
  per-turn spawn.  The pool is an optimization, never a single point of failure.
"""

from __future__ import annotations

import atexit
import json
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


_EOF = object()  # sentinel pushed by the reader thread when stdout closes


class ResidentTurnError(RuntimeError):
    """Raised when a resident turn cannot complete (dead proc / timeout / proto)."""


@dataclass
class _ResidentProc:
    key: str
    workdir: str
    proc: subprocess.Popen
    lock: threading.Lock = field(default_factory=threading.Lock)
    events: "queue.Queue[Any]" = field(default_factory=queue.Queue)
    stderr_tail: "deque[str]" = field(default_factory=lambda: deque(maxlen=40))
    created_at: float = 0.0
    last_used: float = 0.0
    session_id: str | None = None

    def alive(self) -> bool:
        return self.proc.poll() is None


def _reader_thread(proc: subprocess.Popen, events: "queue.Queue[Any]") -> None:
    """Parse stream-json stdout lines into events; push _EOF on close."""
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                events.put(json.loads(line))
            except json.JSONDecodeError:
                # Tolerate non-JSON noise (warnings) without breaking the turn.
                continue
    except Exception:
        pass
    finally:
        events.put(_EOF)


def _stderr_drain_thread(proc: subprocess.Popen, tail: "deque[str]") -> None:
    """Drain stderr so the pipe never blocks; keep a small tail for diagnostics."""
    try:
        for line in proc.stderr:  # type: ignore[union-attr]
            tail.append(line.rstrip("\n"))
    except Exception:
        pass


class ResidentClaudePool:
    """Hold and reuse long-running ``claude`` stream-json processes per session."""

    def __init__(self, *, idle_timeout: float = 1200.0, max_processes: int = 8) -> None:
        self._idle_timeout = max(60.0, float(idle_timeout))
        self._max_processes = max(1, int(max_processes))
        self._pool: dict[str, _ResidentProc] = {}
        self._global_lock = threading.Lock()
        atexit.register(self.shutdown_all)

    # -- lifecycle ---------------------------------------------------------
    def _spawn(
        self,
        *,
        key: str,
        workdir: str,
        claude_bin: str,
        extra_args: list[str],
        env: dict[str, str],
    ) -> _ResidentProc:
        args = [
            claude_bin,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            # Emit incremental assistant text (content_block_delta/text_delta)
            # so the bridge can surface partial output instead of final-only.
            "--include-partial-messages",
            *extra_args,
        ]
        proc = subprocess.Popen(
            args,
            cwd=workdir,
            env=env,
            text=True,
            bufsize=1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        now = time.time()
        entry = _ResidentProc(
            key=key,
            workdir=workdir,
            proc=proc,
            created_at=now,
            last_used=now,
        )
        threading.Thread(
            target=_reader_thread, args=(proc, entry.events), daemon=True
        ).start()
        threading.Thread(
            target=_stderr_drain_thread, args=(proc, entry.stderr_tail), daemon=True
        ).start()
        return entry

    def _terminate(self, entry: _ResidentProc) -> None:
        try:
            if entry.proc.stdin and not entry.proc.stdin.closed:
                entry.proc.stdin.close()
        except Exception:
            pass
        try:
            entry.proc.terminate()
            try:
                entry.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                entry.proc.kill()
        except Exception:
            pass

    def _reap_idle_locked(self) -> None:
        now = time.time()
        for key, entry in list(self._pool.items()):
            if not entry.alive() or (now - entry.last_used) > self._idle_timeout:
                self._terminate(entry)
                self._pool.pop(key, None)

    def _evict_lru_if_needed_locked(self) -> None:
        while len(self._pool) >= self._max_processes:
            oldest_key = min(self._pool, key=lambda k: self._pool[k].last_used)
            self._terminate(self._pool[oldest_key])
            self._pool.pop(oldest_key, None)

    def _drop(self, key: str, entry: _ResidentProc) -> None:
        with self._global_lock:
            if self._pool.get(key) is entry:
                self._pool.pop(key, None)
        self._terminate(entry)

    def shutdown_all(self) -> None:
        with self._global_lock:
            entries = list(self._pool.values())
            self._pool.clear()
        for entry in entries:
            self._terminate(entry)

    # -- turn driving ------------------------------------------------------
    def _get_or_create(
        self,
        *,
        key: str,
        workdir: str,
        claude_bin: str,
        extra_args: list[str],
        env: dict[str, str],
    ) -> tuple[_ResidentProc, bool]:
        with self._global_lock:
            self._reap_idle_locked()
            entry = self._pool.get(key)
            if entry is not None and entry.alive():
                return entry, False
            if entry is not None:
                self._pool.pop(key, None)
            self._evict_lru_if_needed_locked()
            entry = self._spawn(
                key=key,
                workdir=workdir,
                claude_bin=claude_bin,
                extra_args=extra_args,
                env=env,
            )
            self._pool[key] = entry
            return entry, True

    @staticmethod
    def _send(entry: _ResidentProc, text: str) -> None:
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        stdin = entry.proc.stdin
        if stdin is None or stdin.closed:
            raise ResidentTurnError("resident process stdin closed")
        try:
            stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise ResidentTurnError(f"resident process write failed: {exc}") from exc

    @staticmethod
    def _drain_nonblocking(entry: _ResidentProc) -> None:
        while True:
            try:
                entry.events.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _partial_text(event: Any) -> str | None:
        """Extract incremental assistant text from a stream-json event.

        With ``--include-partial-messages`` the CLI wraps raw Anthropic
        streaming events as ``{"type": "stream_event", "event": {...}}``. We
        surface ``content_block_delta`` text deltas; everything else is None.
        """
        if not isinstance(event, dict) or event.get("type") != "stream_event":
            return None
        ev = event.get("event")
        if not isinstance(ev, dict) or ev.get("type") != "content_block_delta":
            return None
        delta = ev.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return None
        text = delta.get("text")
        return text if isinstance(text, str) and text else None

    @classmethod
    def _collect_until_result(
        cls,
        entry: _ResidentProc,
        timeout: float,
        stream_callback: "Callable[[str], None] | None" = None,
    ) -> dict[str, Any]:
        deadline = time.time() + max(1.0, timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ResidentTurnError("resident turn timed out")
            try:
                event = entry.events.get(timeout=min(1.0, remaining))
            except queue.Empty:
                if not entry.alive():
                    raise ResidentTurnError("resident process exited mid-turn")
                continue
            if event is _EOF:
                raise ResidentTurnError("resident process closed stdout mid-turn")
            if isinstance(event, dict) and event.get("type") == "result":
                return event
            if stream_callback is not None:
                partial = cls._partial_text(event)
                if partial:
                    try:
                        stream_callback(partial)
                    except Exception:
                        # A failing consumer must not break the turn.
                        pass

    def run_turn(
        self,
        *,
        key: str,
        workdir: str,
        claude_bin: str,
        extra_args: list[str],
        env: dict[str, str],
        first_prompt: str,
        followup_text: str,
        timeout: float,
        stream_callback: "Callable[[str], None] | None" = None,
    ) -> dict[str, Any]:
        """Drive one turn; return the parsed ``result`` event dict.

        Raises :class:`ResidentTurnError` on any process/protocol failure so the
        caller can respawn-and-retry or fall back to the classic spawn path.
        """
        entry, is_new = self._get_or_create(
            key=key,
            workdir=workdir,
            claude_bin=claude_bin,
            extra_args=extra_args,
            env=env,
        )
        with entry.lock:
            if not entry.alive():
                raise ResidentTurnError("resident process not alive")
            # Fresh process: send the full framing prompt so behaviour matches
            # the classic bridge. Warm process: it already holds the framing in
            # memory, so only the new user message is sent.
            self._drain_nonblocking(entry)
            self._send(entry, first_prompt if is_new else followup_text)
            result = self._collect_until_result(entry, timeout, stream_callback)
            entry.last_used = time.time()
            sid = str(result.get("session_id") or "").strip()
            if sid:
                entry.session_id = sid
            return result

    def invalidate(self, key: str) -> None:
        """Force-drop a session's process (used after a failed turn)."""
        with self._global_lock:
            entry = self._pool.pop(key, None)
        if entry is not None:
            self._terminate(entry)


_POOL: ResidentClaudePool | None = None
_POOL_LOCK = threading.Lock()


def get_pool(*, idle_timeout: float = 1200.0, max_processes: int = 8) -> ResidentClaudePool:
    """Return the process-wide resident pool, creating it on first use."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ResidentClaudePool(
                idle_timeout=idle_timeout, max_processes=max_processes
            )
        return _POOL
