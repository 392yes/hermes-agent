"""Shared active ledger for cross-runtime turn awareness.

hermes-codex and hermes-claude run as separate processes/profiles but
collaborate on the same task. This ledger is a lightweight, append-only
"meeting minutes" both runtimes share: each writes a short summary at the
end of every turn, and at the start of a turn each reads the *peer* runtime's
most recent summaries so it knows what the other side just did.

Design choices (Phase 1):
- Storage: ``<hermes_home>/runtime/active_ledger.jsonl`` (one JSON object per
  line). Chosen over a state.db table for observability (`tail -f`), trivial
  rollback (delete the file), and no schema migration.
- Concurrency: appends are serialized with an advisory file lock (fcntl on
  Unix, msvcrt on Windows). Readers are lock-free and tolerate a partially
  written trailing line by skipping unparseable rows.
- Summaries are produced by callers (heuristic, see turn_finalizer); this
  module only does I/O and never calls an LLM.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# fcntl is Unix-only; on Windows fall back to msvcrt. If neither exists we
# degrade to no locking (single-writer assumption) rather than crashing.
msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific fallback
    fcntl = None
    try:
        import msvcrt  # type: ignore[no-redef]
    except ImportError:
        pass

_LEDGER_RELPATH = "runtime/active_ledger.jsonl"

# Defensive caps. A turn summary is meant to be short; clamp pathological input
# so one bad turn can't bloat the shared ledger. File is trimmed to the most
# recent lines once it grows past the byte ceiling.
_MAX_SUMMARY_CHARS = 2000
_MAX_FILE_BYTES = 1_000_000
_TRIM_TO_LINES = 300


@dataclass(frozen=True)
class LedgerEntry:
    """One turn's contribution to the shared ledger (immutable)."""

    ts: str
    runtime: str
    summary: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    turn_id: Optional[str] = None
    end_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "runtime": self.runtime,
            "summary": self.summary,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "end_reason": self.end_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerEntry":
        return cls(
            ts=str(data.get("ts") or ""),
            runtime=str(data.get("runtime") or ""),
            summary=str(data.get("summary") or ""),
            session_id=_opt_str(data.get("session_id")),
            task_id=_opt_str(data.get("task_id")),
            turn_id=_opt_str(data.get("turn_id")),
            end_reason=_opt_str(data.get("end_reason")),
        )


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_path(hermes_home: Optional[Path] = None) -> Path:
    base = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return base / _LEDGER_RELPATH


@contextmanager
def _ledger_lock(path: Path) -> Iterator[None]:
    """Serialize ledger writes across processes via an advisory lock file."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:  # pragma: no cover - rare platform
        yield
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows path
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        elif msvcrt:  # pragma: no cover - Windows path
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        fd.close()


def _clamp_summary(summary: str) -> str:
    text = " ".join(str(summary or "").split())
    if len(text) > _MAX_SUMMARY_CHARS:
        return text[: _MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return text


def write_turn(
    *,
    runtime: str,
    summary: str,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    end_reason: Optional[str] = None,
    hermes_home: Optional[Path] = None,
) -> bool:
    """Append one turn summary to the shared ledger.

    Best-effort: returns True on success, False on any failure (callers should
    never let a ledger error break the turn). ``runtime`` is the writer's own
    identity (e.g. "codex" / "claude").
    """
    runtime_id = str(runtime or "").strip()
    clamped = _clamp_summary(summary)
    if not runtime_id or not clamped:
        return False

    entry = LedgerEntry(
        ts=_now_iso(),
        runtime=runtime_id,
        summary=clamped,
        session_id=_opt_str(session_id),
        task_id=_opt_str(task_id),
        turn_id=_opt_str(turn_id),
        end_reason=_opt_str(end_reason),
    )
    line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"

    try:
        path = _ledger_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _ledger_lock(path):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            _maybe_trim(path)
        return True
    except Exception:  # pragma: no cover - defensive; ledger must never throw
        logger.debug("active ledger write failed", exc_info=True)
        return False


def read_peer_recent(
    *,
    self_runtime: str,
    limit: int = 1,
    hermes_home: Optional[Path] = None,
) -> List[LedgerEntry]:
    """Return the most recent ledger entries written by *other* runtimes.

    Entries whose ``runtime`` equals ``self_runtime`` are excluded (a runtime
    should not be fed its own minutes). Most recent first. Returns [] on any
    error or empty/missing ledger.
    """
    self_id = str(self_runtime or "").strip()
    want = max(1, int(limit))
    try:
        path = _ledger_path(hermes_home)
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
    except Exception:  # pragma: no cover - defensive
        logger.debug("active ledger read failed", exc_info=True)
        return []

    peers: List[LedgerEntry] = []
    # Walk newest-first so we can stop as soon as we have enough.
    for raw in reversed(text.splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Tolerate a partially written trailing line.
            continue
        if not isinstance(data, dict):
            continue
        entry = LedgerEntry.from_dict(data)
        if not entry.runtime or entry.runtime == self_id:
            continue
        peers.append(entry)
        if len(peers) >= want:
            break
    return peers


def _maybe_trim(path: Path) -> None:
    """Keep the ledger bounded. Caller must already hold the lock."""
    try:
        if path.stat().st_size <= _MAX_FILE_BYTES:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= _TRIM_TO_LINES:
            return
        kept = lines[-_TRIM_TO_LINES:]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:  # pragma: no cover - defensive
        logger.debug("active ledger trim failed", exc_info=True)
