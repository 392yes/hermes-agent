"""Hugo/Clara orchestrator mode state and command parsing.

This is intentionally a small gateway-side control plane.  It does not change
Hermes' configured provider/model; it changes the role policy injected into
future gateway turns so subscription-backed Codex CLI and Claude Code CLI can be
used as runtimes without accidentally routing through API billing providers.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - import-safe fallback for isolated tests
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        return Path.home() / ".hermes"

MODE_HUGO_LEAD = "hugo-lead"
MODE_CLARA_LEAD = "clara-lead"
VALID_MODES = {MODE_HUGO_LEAD, MODE_CLARA_LEAD}

_MODE_FILE = "runtime/orchestrator-mode.json"

# Per-process lead-mode pin.  When set (e.g. `HERMES_LEAD_MODE=hugo-lead hermes`
# in one Wave pane), this process ignores the global mode file for reads and
# refuses mode switches, so two panes can run different leads concurrently
# without flipping each other or the Slack gateway.  The gateway daemon must be
# launched WITHOUT this variable so Slack keeps following the global file.
ENV_LEAD_MODE = "HERMES_LEAD_MODE"


def env_pinned_mode() -> str | None:
    """Return the lead mode pinned via HERMES_LEAD_MODE for this process, if any."""
    return normalize_mode(os.environ.get(ENV_LEAD_MODE))


_HUGO_ALIASES = {
    "1",
    "1번",
    "1번 모드",
    "기본 모드",
    "기본 모드로",
    "기본모드",
    "기본모드로",
    "hugo",
    "hugo-lead",
    "hugo lead",
    "휴고",
    "휴고 주도",
    "휴고 주도 모드",
    "휴고 리드",
    "휴고 리드 모드",
}

_CLARA_ALIASES = {
    "2",
    "2번",
    "2번 모드",
    "전환 모드",
    "전환모드",
    "clara",
    "clara-lead",
    "clara lead",
    "claude lead",
    "claude-code lead",
    "클라라",
    "클라라 주도",
    "클라라 주도 모드",
    "클라라 주도 모드로",
    "클라라 리드",
    "클라라 리드 모드",
    "클로드 주도",
    "클로드 주도 모드",
}

_STATUS_ALIASES = {
    "현재 모드",
    "현재모드",
    "모드 뭐야",
    "모드 뭐야?",
    "지금 모드",
    "지금 모드 뭐야",
}


def mode_path(hermes_home: Path | None = None) -> Path:
    home = Path(hermes_home) if hermes_home is not None else Path(get_hermes_home())
    return home / _MODE_FILE


def normalize_mode(value: str | None) -> str | None:
    text = str(value or "").strip().casefold().replace("_", "-")
    if text in {"1", "hugo", "hugo-lead", "hugo lead", "default", "기본", "기본 모드", "휴고", "휴고 주도"}:
        return MODE_HUGO_LEAD
    if text in {"2", "clara", "clara-lead", "clara lead", "claude", "claude lead", "클라라", "클라라 주도", "클로드", "클로드 주도"}:
        return MODE_CLARA_LEAD
    if text in VALID_MODES:
        return text
    return None


def read_mode(hermes_home: Path | None = None) -> dict[str, Any]:
    pinned = env_pinned_mode()
    if pinned:
        return {"mode": pinned, "source": f"env:{ENV_LEAD_MODE}", "pinned": True}
    path = mode_path(hermes_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    mode = normalize_mode(str(data.get("mode") or "")) or MODE_HUGO_LEAD
    data["mode"] = mode
    return data


def write_mode(mode: str, *, hermes_home: Path | None = None, source: str = "gateway") -> dict[str, Any]:
    normalized = normalize_mode(mode)
    if normalized not in VALID_MODES:
        raise ValueError(f"unknown orchestrator mode: {mode}")
    path = mode_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "mode": normalized,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data


def describe_mode(mode: str | None = None, *, hermes_home: Path | None = None) -> str:
    current = normalize_mode(mode) or read_mode(hermes_home).get("mode") or MODE_HUGO_LEAD
    if current == MODE_CLARA_LEAD:
        return (
            "현재 모드: 2번 `clara-lead`\n"
            "- Clara/Claude Code: 오케스트레이터 + 코딩 실행\n"
            "- 의견종합/대표 보고: Clara 단독 (보조 의견은 Clara가 취합해 하나의 보고로)\n"
            "- 일반 응답 포함 모든 기본 응답 경로: Claude Code CLI bridge(Claude 구독)\n"
            "- Codex: 리뷰/테스트\n"
            "- 런타임 원칙: Claude Code CLI는 Claude 구독, Codex CLI는 ChatGPT/Codex 구독만 사용"
        )
    return (
        "현재 모드: 1번 `hugo-lead`\n"
        "- Hugo/Codex: 오케스트레이터 + 코딩 실행\n"
        "- 의견종합/대표 보고: Hugo 단독 (보조 의견은 Hugo가 취합해 하나의 보고로)\n"
        "- Claude Code: 리뷰/테스트\n"
        "- 런타임 원칙: Codex CLI는 ChatGPT/Codex 구독, Claude Code CLI는 Claude 구독만 사용"
    )


def mode_system_note(hermes_home: Path | None = None) -> str:
    mode = read_mode(hermes_home).get("mode")
    if mode == MODE_CLARA_LEAD:
        return (
            "[Orchestrator mode: 2번 clara-lead]\n"
            "Clara/Claude Code takes Hugo's normal lead role in this mode: receive the request, plan, execute, code, verify, coordinate helpers, and report the result. "
            "Route every normal assistant turn, including general chat replies, to the official local Claude Code CLI (`claude -p`) so the Claude subscription is used; this is not a display-only mode. "
            "Codex CLI is the designated reviewer/tester in this mode (run via its ChatGPT/Codex subscription login); route review/test passes to Codex, mirroring how Claude Code is the reviewer in hugo-lead. The primary response and coding path remains Claude Code CLI. "
            "Opinion synthesis: Clara alone collects and synthesizes all helper opinions into one consolidated report; helpers never report to the user separately in this mode. "
            "Do not switch Hermes to the Anthropic API provider for this mode. Keep all external side effects within the user's requested target and scope. "
            "Continuity rule: clara-lead uses the same canonical Slack session history, default Hermes session_search/state.db, memory, and active project context as hugo-lead; do not treat lead-mode switches as a loss of prior project context."
        )
    return (
        "[Orchestrator mode: 1번 hugo-lead]\n"
        "Hugo/Codex takes the lead role in this mode: receive the request, plan, execute, code, verify, coordinate helpers, and report the result. Route normal assistant turns and implementation to the Codex CLI runtime so the ChatGPT/Codex subscription is used. Claude Code CLI is the designated reviewer/tester in this mode (run via its Claude subscription); route review/test/security passes to Claude Code, mirroring how Codex is the reviewer in clara-lead. The primary response and coding path remains Codex CLI. "
        "Opinion synthesis: Hugo alone collects and synthesizes all helper opinions into one consolidated report; helpers never report to the user separately in this mode. "
        "Do not switch Hermes to the Anthropic API provider for Claude Code subscription work. Do not push, deploy, publish, rewrite history, or delete production/business data without explicit approval. "
        "Continuity rule: hugo-lead uses the same canonical Slack session history, default Hermes session_search/state.db, memory, and active project context as clara-lead; do not treat lead-mode switches as a loss of prior project context."
    )


def _clean_text(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", cleaned).strip()
    cleaned = re.sub(r"^<@[^>]+>\s*", "", cleaned).strip()
    cleaned = cleaned.strip("`'\"“”‘’ 。.!?\t\n")
    return re.sub(r"\s+", " ", cleaned).casefold()


def parse_mode_request(text: str) -> tuple[str, str | None] | None:
    """Return (action, mode) for natural Korean/English mode commands.

    action is one of: ``set`` or ``status``.  ``mode`` is set only for ``set``.
    The parser is intentionally conservative: it matches exact short commands
    and common phrases, not arbitrary sentences that merely mention a mode.
    """
    cleaned = _clean_text(text)
    if not cleaned:
        return None
    if cleaned in _STATUS_ALIASES:
        return ("status", None)
    if cleaned in _HUGO_ALIASES:
        return ("set", MODE_HUGO_LEAD)
    if cleaned in _CLARA_ALIASES:
        return ("set", MODE_CLARA_LEAD)

    # Common explicit non-slash forms: "모드 1", "mode 2".
    m = re.fullmatch(r"(?:모드|mode)\s*[:=]?\s*(.+)", cleaned)
    if m:
        value = m.group(1).strip()
        if value in _STATUS_ALIASES:
            return ("status", None)
        mode = normalize_mode(value)
        if mode:
            return ("set", mode)
    return None


def _pinned_switch_notice() -> str | None:
    pinned = env_pinned_mode()
    if not pinned:
        return None
    return (
        f"이 세션은 {ENV_LEAD_MODE}={pinned} 로 고정(pinned)되어 있어 모드를 전환하지 않았습니다.\n"
        "이 pane은 계속 고정된 lead로 동작하며, 전역 모드 전환은 고정 없이 실행된 세션이나 Slack에서 해주세요."
    )


def handle_mode_text(text: str, *, hermes_home: Path | None = None, source: str = "gateway") -> str | None:
    parsed = parse_mode_request(text)
    if parsed is None:
        return None
    action, mode = parsed
    if action == "status":
        return describe_mode(hermes_home=hermes_home)
    assert mode is not None
    pinned_notice = _pinned_switch_notice()
    if pinned_notice:
        return pinned_notice
    write_mode(mode, hermes_home=hermes_home, source=source)
    return "✓ 오케스트레이터 운용 모드를 전환했습니다.\n\n" + describe_mode(mode, hermes_home=hermes_home)
