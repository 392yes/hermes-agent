"""Claude Code CLI bridge for gateway-routed Clara turns.

This module intentionally uses the official local ``claude`` CLI instead of
Anthropic's API provider.  It lets a Slack role/profile route run through the
user's Claude Code subscription login while keeping the gateway response path
simple and auditable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_PROVIDER_NAMES = {"claude-code-cli", "claude_code_cli", "claude-cli", "claude_cli"}
_DEFAULT_ALLOWED_TOOLS = (
    "Read,"
    "Write,"
    "Edit,"
    "MultiEdit,"
    "Glob,"
    "Grep,"
    "LS,"
    "Bash(git status*),"
    "Bash(git diff*),"
    "Bash(git log*),"
    "Bash(git show*),"
    "Bash(git branch*),"
    "Bash(git ls-files*),"
    "Bash(pytest *),"
    "Bash(python -m pytest*),"
    "Bash(uv run pytest*),"
    "Bash(npm test*),"
    "Bash(npm run *),"
    "Bash(pnpm test*),"
    "Bash(pnpm run *),"
    "Bash(yarn test*),"
    "Bash(yarn run *),"
    "Bash(python *),"
    "Bash(node *),"
    "Bash(ls *),"
    "Bash(find *),"
    "Bash(grep *),"
    "Bash(rg *)"
)
_SECRET_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_API_KEY",
)
_CLAUDE_SESSION_MAP = "runtime/claude-code-bridge-sessions.json"
_ENV_DISABLE_RESUME = "HERMES_CLARA_DISABLE_RESUME"


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "enabled"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _split_allowed_tools(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _is_resident_startup_execution_error(parsed: dict[str, Any] | None) -> bool:
    """Return True for Claude Code stream-json startup/runtime failures.

    Claude Code can emit a final ``result`` event with ``subtype``
    ``error_during_execution`` before any model turn starts.  In that shape the
    resident process did not produce a useful answer and must be treated like a
    process/protocol failure so the bridge can invalidate the warm process and
    retry/fall back instead of surfacing a failed Clara job to the user.
    """
    if not isinstance(parsed, dict):
        return False
    if str(parsed.get("subtype") or "") != "error_during_execution":
        return False
    if int(parsed.get("num_turns") or 0) != 0:
        return False
    usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    model_usage = parsed.get("modelUsage") if isinstance(parsed.get("modelUsage"), dict) else {}
    no_tokens = not any(
        int(usage.get(key) or 0)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    return no_tokens and not model_usage


@dataclass
class ClaudeCodeBridgeResult:
    final_response: str
    job_id: str
    workdir: str
    log_dir: str
    exit_code: int
    raw_json: dict[str, Any] | None = None
    interrupted: bool = False


def is_claude_code_cli_config(config: dict[str, Any] | None) -> bool:
    """Return True when a routed profile should use Claude Code CLI."""
    if not isinstance(config, dict):
        return False
    model_cfg = config.get("model") or {}
    provider = ""
    if isinstance(model_cfg, dict):
        provider = str(model_cfg.get("provider") or "").strip().casefold()
    elif isinstance(model_cfg, str):
        provider = str(model_cfg).strip().casefold()
    if provider in _PROVIDER_NAMES:
        return True
    bridge_cfg = config.get("claude_code_cli") or config.get("clara_cli") or {}
    return isinstance(bridge_cfg, dict) and bool(bridge_cfg.get("enabled"))


# ---------------------------------------------------------------------------
# Runtime model override (/model-swap)
# ---------------------------------------------------------------------------
# The `/model-swap` slash command lets a single interactive pane switch the
# Clara bridge model (e.g. opus <-> fable) mid-session without restarting the
# launcher. It writes a small JSON file that this module reads on every turn
# and overlays onto the merged bridge config, so the resolved `--model` wins
# over both config.yaml (clara_cli.model) and the inherited ANTHROPIC_MODEL env.
# Continuity is preserved by the normal per-turn history re-injection; the
# swap handler evicts the resident pool so the next turn respawns cold with
# the new model.

# Friendly aliases -> concrete Claude model ids.
CLARA_MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "opus-4.8": "claude-opus-4-8",
    "fable": "claude-fable-5",
    "fable-5": "claude-fable-5",
}


def clara_model_override_path() -> str:
    """Path of the runtime model-override file for the Clara bridge."""
    base = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(base, "runtime", "clara-model-override.json")


def read_clara_model_override_meta() -> dict[str, Any]:
    """Return the full runtime model-override record, or {} when none is set."""
    try:
        with open(clara_model_override_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def read_clara_model_override() -> str:
    """Return the current runtime model override, or '' when none is set."""
    return str(read_clara_model_override_meta().get("model") or "").strip()


def write_clara_model_override(model: str, from_model: str | None = None) -> None:
    """Persist the runtime model override. Empty model clears the override.

    ``from_model`` records the effective model that was active immediately
    before the swap so the next turn can emit an auto-handoff banner telling
    the freshly-spawned model to treat the re-injected history as its own
    continuous context (no manual /session-handoff required).
    """
    path = clara_model_override_path()
    normalized = str(model or "").strip()
    if not normalized:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    record: dict[str, Any] = {"model": normalized, "swapped_at": time.time()}
    prior = str(from_model or "").strip()
    if prior and prior != normalized:
        record["from_model"] = prior
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False)
    os.replace(tmp, path)


def clara_handoff_pending() -> bool:
    """True when a /model-swap override is active but its one-shot 100% handoff
    has not yet been performed.

    The one-shot handoff force-resumes the previous model's native Claude Code
    session under the new model on the first turn after a swap, so the new model
    inherits the FULL prior conversation (turns + tool calls + tool results +
    file state), not just a truncated text re-injection.
    """
    meta = read_clara_model_override_meta()
    return bool(meta.get("model")) and not bool(meta.get("handed_off"))


def mark_clara_handoff_done() -> None:
    """Mark the active model-swap override's one-shot handoff as consumed.

    No-op when no override is set or it is already consumed. Rewrites the record
    immutably (spreads existing fields) so a later ``off`` / new swap resets it.
    Each fresh ``write_clara_model_override`` omits ``handed_off``, so a new swap
    naturally re-arms the one-shot handoff.
    """
    meta = read_clara_model_override_meta()
    if not meta.get("model") or meta.get("handed_off"):
        return
    updated = {**meta, "handed_off": True, "handed_off_at": time.time()}
    path = clara_model_override_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(updated, fh, ensure_ascii=False)
    os.replace(tmp, path)


def resolve_clara_model_alias(value: str) -> str:
    """Map a friendly alias (opus/fable) to a concrete model id, else passthrough."""
    normalized = str(value or "").strip()
    return CLARA_MODEL_ALIASES.get(normalized.lower(), normalized)


def bridge_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Merge supported Claude Code bridge config sections."""
    merged: dict[str, Any] = {}
    if isinstance(config, dict):
        for key in ("claude_code_cli", "clara_cli"):
            value = config.get(key)
            if isinstance(value, dict):
                merged.update(value)
    # Runtime /model-swap override wins over the static config model.
    override = read_clara_model_override()
    if override:
        merged["model"] = override
    return merged


def _expand_path(value: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(value)))


def extract_explicit_workdir(message: str) -> str | None:
    """Find an explicit existing absolute directory path in a Slack prompt."""
    text = str(message or "")
    # Keep this conservative: only absolute user/project-ish paths, stop at
    # whitespace or common punctuation used in Korean/Slack prompts.
    for match in re.finditer(r"(/Users/[^\s`'\"<>，,]+|/Volumes/[^\s`'\"<>，,]+|/private/[^\s`'\"<>，,]+)", text):
        candidate = match.group(1).rstrip("。、.,:;)]}")
        try:
            path = Path(_expand_path(candidate))
            if path.is_dir():
                return str(path)
        except Exception:
            continue
    return None


def resolve_workdir(config: dict[str, Any] | None, message: str, hermes_home: Path | None = None) -> str:
    """Resolve the cwd for Claude Code, preferring explicit prompt/config context."""
    explicit = extract_explicit_workdir(message)
    if explicit:
        return explicit
    bcfg = bridge_config(config)
    for key in ("workdir", "default_workdir", "cwd"):
        value = bcfg.get(key)
        if value:
            path = Path(_expand_path(str(value)))
            if path.is_dir():
                return str(path)
    terminal_cfg = (config or {}).get("terminal") if isinstance(config, dict) else None
    if isinstance(terminal_cfg, dict) and terminal_cfg.get("cwd"):
        path = Path(_expand_path(str(terminal_cfg.get("cwd"))))
        if path.is_dir():
            return str(path)
    return os.getcwd()


def _is_error_max_turns(parsed: dict[str, Any] | None, stderr: str = "", stdout: str = "") -> bool:
    """Detect Claude Code's max-turns stop condition across JSON/log variants."""
    if isinstance(parsed, dict):
        values = [
            parsed.get("subtype"),
            parsed.get("error"),
            parsed.get("error_type"),
            parsed.get("type"),
            parsed.get("message"),
            parsed.get("result"),
        ]
        if any("error_max_turns" in str(value) for value in values if value is not None):
            return True
    combined = f"{stderr}\n{stdout}".casefold()
    return "error_max_turns" in combined or "reached maximum number of turns" in combined


def _is_quota_or_spend_limit(parsed: dict[str, Any] | None, stderr: str = "", stdout: str = "") -> bool:
    """Detect Claude Code account quota/spend-limit failures."""
    if isinstance(parsed, dict):
        status = parsed.get("api_error_status")
        if str(status).strip() == "429":
            return True
        values = [
            parsed.get("subtype"),
            parsed.get("error"),
            parsed.get("error_type"),
            parsed.get("type"),
            parsed.get("message"),
            parsed.get("result"),
        ]
        if any("monthly spend limit" in str(value).casefold() for value in values if value is not None):
            return True
    combined = f"{stderr}\n{stdout}".casefold()
    return "monthly spend limit" in combined or "api_error_status\":429" in combined


def _is_auth_failed(parsed: dict[str, Any] | None, stderr: str = "", stdout: str = "") -> bool:
    """Detect Claude Code authentication failures across CLI/SDK result shapes."""
    if isinstance(parsed, dict):
        status = parsed.get("api_error_status")
        if str(status).strip() == "401":
            return True
        values = [
            parsed.get("subtype"),
            parsed.get("error"),
            parsed.get("error_type"),
            parsed.get("type"),
            parsed.get("message"),
            parsed.get("result"),
            parsed.get("sdk_exception"),
        ]
        if any("invalid authentication credentials" in str(value).casefold() for value in values if value is not None):
            return True
    combined = f"{stderr}\n{stdout}".casefold()
    return "invalid authentication credentials" in combined or "api_error_status\":401" in combined or "authentication_failed" in combined


def _format_failure_result(
    *,
    parsed: dict[str, Any] | None,
    stderr: str,
    stdout: str,
    job_id: str,
    exit_code: int,
    log_dir: Path,
    max_turns: int,
) -> str:
    """Render a user-facing Clara bridge failure/continuation message."""
    tail = "\n".join((stderr or stdout).splitlines()[-12:]).strip()
    if _is_error_max_turns(parsed, stderr, stdout):
        result_text = (
            "⏸️ Clara Claude Code CLI 작업이 실패한 것이 아니라 작업 제한(max_turns)에 도달했습니다.\n"
            "이전 작업 로그/맥락이 남아 있으므로 같은 요청을 이어서 진행할 수 있습니다.\n"
            f"현재 제한: max_turns={max_turns}\n"
            f"job_id: {job_id}\n"
            f"exit_code: {exit_code}\n"
            f"log_dir: {log_dir}\n"
        )
    elif _is_quota_or_spend_limit(parsed, stderr, stdout):
        result_text = (
            "⚠️ Clara Claude Code CLI가 Claude 계정 월 사용 한도에 걸려 실행되지 못했습니다.\n"
            "Claude 한도를 올리거나 다음 결제 주기까지 기다려야 Claude Code CLI 경로를 다시 사용할 수 있습니다.\n"
            "즉시 작업을 계속하려면 새 Wave pane에서 `hermes-hugo`를 실행해 Hugo/Codex 작업대로 진행하세요.\n"
            f"job_id: {job_id}\n"
            f"exit_code: {exit_code}\n"
            f"log_dir: {log_dir}\n"
        )
    elif _is_auth_failed(parsed, stderr, stdout):
        result_text = (
            "⚠️ Clara Claude Code 인증이 실패했습니다.\n"
            "Claude Code 로그인 상태는 남아 있지만 API가 401 Invalid authentication credentials를 반환했습니다.\n"
            "해결: `claude auth login` 또는 `claude setup-token`으로 Claude Code 인증을 갱신해야 합니다.\n"
            f"job_id: {job_id}\n"
            f"exit_code: {exit_code}\n"
            f"log_dir: {log_dir}\n"
        )
    else:
        result_text = (
            "⚠️ Clara Claude Code CLI 작업이 실패했습니다.\n"
            f"job_id: {job_id}\n"
            f"exit_code: {exit_code}\n"
            f"log_dir: {log_dir}\n"
        )
    if tail:
        result_text += f"\n최근 로그:\n{tail}"
    return result_text


def _last_history(history: Iterable[dict[str, Any]], limit: int) -> list[dict[str, str]]:
    usable: list[dict[str, str]] = []
    for item in history or []:
        role = str(item.get("role") or "")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            usable.append({"role": role, "content": str(content)})
    return usable[-max(0, limit):]


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _message_to_text(message: Any) -> str:
    """Flatten a user message to plain text.

    ``message`` is normally a string, but when the user attaches an image to a
    vision-capable model it becomes a list of OpenAI-style content parts
    (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``). Passing that
    list straight into ``re.findall`` raised ``TypeError: expected string or
    bytes-like object, got 'list'`` — e.g. pasting an image with no text. Here
    we keep only the text parts; image-only messages collapse to ``""``.
    """
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        texts: list[str] = []
        for part in message:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(t for t in texts if t)
    return str(message or "")


def _extract_search_terms(message: Any, workdir: str | None, wave_context: dict[str, Any]) -> list[str]:
    """Return conservative FTS terms for continuity lookup."""
    candidates: list[str] = []
    for key in ("project_name", "project_path"):
        value = str(wave_context.get(key) or "").strip()
        if value:
            candidates.append(value)
    if workdir:
        candidates.extend([workdir, Path(workdir).name])
    candidates.append(_message_to_text(message))

    terms: list[str] = []
    seen: set[str] = set()
    stop = {
        "this", "that", "with", "from", "have", "mode", "lead",
        "프로젝트", "대화", "모드", "참조", "이전", "계속", "정확하게",
    }
    for text in candidates:
        for token in re.findall(r"[A-Za-z0-9_가-힣]{3,}", text):
            t = token.strip("_").casefold()
            if not t or t in stop or t in seen:
                continue
            seen.add(t)
            terms.append(token)
            if len(terms) >= 8:
                return terms
    return terms


def _canonical_hermes_home(hermes_home: Path) -> Path:
    try:
        from gateway.orchestrator_modes import canonical_hermes_home
        return canonical_hermes_home(hermes_home)
    except Exception:
        try:
            if hermes_home.parent.name == "profiles" and hermes_home.parent.parent.name == ".hermes":
                return hermes_home.parent.parent
        except Exception:
            pass
        return hermes_home


def _find_repo_root(start: str | None) -> Path | None:
    if not start:
        return None
    try:
        path = Path(start).expanduser().resolve()
    except Exception:
        return None
    if not path.is_dir():
        return None
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _latest_obsidian_handoffs(limit: int = 3) -> list[Path]:
    root = Path.home() / "Library/CloudStorage/OneDrive-Personal/OneSyncFiles/AI-Sessions/handover"
    try:
        files = [p for p in root.glob("*.md") if p.is_file()]
    except Exception:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[: max(1, int(limit))]


def _handover_context_lines(workdir: str | None) -> list[str]:
    """Return handoff/resume continuity hints shared by Hugo and Clara."""
    candidates: list[Path] = []
    repo_root = _find_repo_root(workdir)
    if repo_root:
        candidates.append(repo_root / "handover.md")
    if workdir:
        candidates.append(Path(workdir).expanduser() / "handover.md")
    existing: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        key = str(resolved)
        if key not in seen and path.exists() and path.is_file():
            seen.add(key)
            existing.append(path)

    obsidian = _latest_obsidian_handoffs()
    if not existing and not obsidian:
        return []

    lines = [
        "\nSession handoff continuity:",
        "- /session-handoff writes canonical handover.md plus an Obsidian copy; /session-resume must read that file before continuing work.",
        "- HERMES_CLARA_DISABLE_RESUME only disables Claude Code native session resume; it does not disable handover.md / Obsidian file continuity shared with Hugo.",
    ]
    if existing:
        lines.append("- Canonical handover candidates:")
        lines.extend(f"  - {p}" for p in existing)
    if obsidian:
        lines.append("- Latest Obsidian handover copies:")
        lines.extend(f"  - {p}" for p in obsidian)
    return lines


def _query_recent_session_snippets(hermes_home: Path, terms: list[str], limit: int = 4) -> list[str]:
    db_path = _canonical_hermes_home(hermes_home) / "state.db"
    if not db_path.exists() or not terms:
        return []
    query = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    snippets: list[str] = []
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT m.session_id, m.role, m.content, m.timestamp, COALESCE(s.title, '')
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            LEFT JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
              AND m.role IN ('user', 'assistant')
              AND m.content IS NOT NULL
            ORDER BY m.timestamp DESC
            LIMIT ?
            """,
            (query, max(1, int(limit))),
        ).fetchall()
    except Exception:
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    for session_id, role, content, _ts, title in rows:
        compact = " ".join(str(content or "").split())[:500]
        if not compact:
            continue
        label = f"{title or session_id} / {role}"
        snippets.append(f"- {label}: {compact}")
    return snippets


def build_continuity_context(*, hermes_home: Path, message: str, workdir: str | None = None) -> str:
    """Build a mode-independent continuity packet for Claude Code bridge turns.

    Claude Code CLI runs outside Hermes' tool loop, so it cannot call the
    ``session_search`` tool directly.  This packet gives clara-lead the same
    canonical project/session continuity that hugo-lead can retrieve from the
    default Hermes store.
    """
    canonical_home = _canonical_hermes_home(hermes_home)

    lines = [
        "## Mode-independent continuity context",
        "hugo-lead and clara-lead must use the same canonical conversation/project continuity. Future lead bots/profiles must follow the same invariant: lead mode changes who orchestrates and which runtime is used, not which prior conversations, memory, project context, or operating policy are relevant.",
        f"Canonical Hermes home/session DB: {canonical_home}",
        "Use this context as a starting point. If it is insufficient and you have local file/shell access, inspect project files and/or ~/.hermes/state.db rather than assuming prior context is unavailable.",
    ]
    swap_meta = read_clara_model_override_meta()
    if swap_meta.get("model"):
        current_model = str(swap_meta.get("model") or "").strip()
        prior_model = str(swap_meta.get("from_model") or "").strip()
        handoff = [
            "\n## In-session model handoff (/model-swap) — CONTINUE SEAMLESSLY",
            (
                f"This pane switched its running model to '{current_model}'"
                + (f" (previously '{prior_model}')" if prior_model else "")
                + " mid-conversation via /model-swap."
            ),
            "The conversation history re-injected below is YOUR OWN prior context in this same pane and task.",
            "Continue the ongoing work directly. Do NOT restart, re-introduce yourself, or ask the user to repeat what was already discussed. Carry the task forward from where the previous turn left off.",
        ]
        if not swap_meta.get("handed_off"):
            handoff.append(
                "This is the first turn after the swap: the previous model's FULL native Claude session "
                "(all turns, tool calls, tool results, and file state) is being resumed under you now, "
                "so treat it as a complete, seamless takeover — nothing was lost in the switch."
            )
        lines.extend(handoff)
    lines.extend(_handover_context_lines(workdir))
    terms = _extract_search_terms(message, workdir, {})
    snippets = _query_recent_session_snippets(canonical_home, terms)
    if snippets:
        lines.append("\nRecent matching conversation snippets from the canonical Hermes session store:")
        lines.extend(snippets)

    # Shared active ledger: surface the peer runtime's (native codex) recent
    # turn so Clara sees what Hugo/Codex just did. Best-effort.
    try:
        from agent import team_active_ledger

        peer_block = team_active_ledger.peer_context_block(
            self_runtime=team_active_ledger.RUNTIME_CLAUDE,
            hermes_home=hermes_home,
        )
        if peer_block:
            lines.append("\n" + peer_block)
    except Exception:
        pass  # ledger is best-effort

    return "\n".join(lines)


def build_claude_prompt(
    *,
    message: str,
    context_prompt: str | None = None,
    channel_prompt: str | None = None,
    history: Iterable[dict[str, Any]] | None = None,
    history_limit: int = 6,
    workdir: str | None = None,
    role_mode: str | None = None,
    continuity_context: str | None = None,
) -> str:
    """Build a single Claude Code prompt from gateway context."""
    normalized_role = str(role_mode or "reviewer").strip().casefold().replace("_", "-")
    if normalized_role in {"lead", "clara-lead", "orchestrator", "coder"}:
        role_lines = [
            "You are Clara/클라라, Sangkun Lee's lead orchestrator and coding manager.",
            "Clara is the coding orchestrator, not a review-only role.",
            "For simple explanation, diagnosis, or decision-support questions, answer directly and avoid broad repository/tool exploration unless needed.",
            "For implementation/debugging tasks, inspect, edit, run, verify, and report end-to-end.",
            "Operating mode: 2번 clara-lead. In this mode you take Hugo's normal lead role: receive the request, plan, execute, code, verify, coordinate helpers, and report the result.",
            "Use the official Claude Agent SDK subscription runtime as your execution environment. Do not route hermes-claude turns through raw Claude CLI unless Sangkun explicitly approves a fallback.",
            "Symmetry rule: mirror hermes-codex/hugo-lead behavior. Clara is the lead voice, but do not suppress helper/reviewer progress, findings, or evidence; include them as review/test inputs and then give Clara's synthesized conclusion.",
        ]
    else:
        role_lines = [
            "You are Clara/클라라, Sangkun Lee's review, testing, and security gate.",
            "Operating mode: 1번 hugo-lead review/test gate unless the user explicitly asks you to implement local changes.",
        ]
    parts: list[str] = [
        *role_lines,
        "Respond in Korean by default. Be concise, concrete, and action-oriented.",
        "Operating authority: when you are in clara-lead mode, use the same operational authority Sangkun expects from Hugo: inspect, edit, run commands, coordinate work, and complete the task end-to-end within the user's requested scope.",
        "When working inside the assigned repository, Obsidian vault, project folder, or Hermes profile scope, directly create/edit/patch/refactor/remove local files needed for implementation, review, testing, documentation, and fixes.",
        "If a problem is clear and local file edits are appropriate, make the change yourself, run relevant verification, and report what changed instead of only giving a repair prompt.",
        "Safety boundary inherited from Hugo: preserve user work, do not expose secrets, and keep external side effects such as push/deploy/publish/production writes within the user's requested target and scope.",
    ]
    if workdir:
        parts.append(f"Working directory: {workdir}")
    if channel_prompt:
        if normalized_role in {"lead", "clara-lead", "orchestrator", "coder"}:
            parts.append(
                "\nSlack role/channel instruction:\n"
                "Always start every Slack reply in #office with this exact role marker on the first line: "
                "'🟪 Clara/클라라 — '. You are Clara/클라라 reporting as the lead orchestrator. "
                "Do not use the Hugo/휴고 marker in clara-lead mode, even if older channel or history context mentions Hugo. "
                "Post as Clara lead, but keep parity with hermes-codex: include helper/reviewer findings as evidence when they are relevant, then provide Clara's synthesized conclusion."
            )
        else:
            parts.append("\nSlack role/channel instruction:\n" + str(channel_prompt))
    else:
        parts.append(
            "\nCLI output instruction:\n"
            "Mirror hermes-codex terminal style. Do not use Slack role markers or Slack-ready phrasing. "
            "Use concise title/list text that looks natural inside the Hermes CLI response box, and include checked items, findings, verification, and next action when relevant."
        )
    if context_prompt:
        parts.append("\nHermes context prompt:\n" + str(context_prompt))
    if continuity_context:
        parts.append("\n" + str(continuity_context))
    hist = _last_history(history or [], history_limit)
    if hist:
        rendered = []
        for msg in hist:
            rendered.append(f"{msg['role']}: {msg['content']}")
        parts.append("\nRecent Slack conversation context:\n" + "\n---\n".join(rendered))
    request_text = _message_to_text(message)
    if not request_text.strip():
        # Image-only paste (no text parts): give Claude an explicit cue instead
        # of an empty request or a raw content-parts list repr.
        request_text = "(사용자가 텍스트 없이 이미지/첨부만 보냈습니다.)"
    parts.append("\nCurrent user request:\n" + request_text)
    if channel_prompt:
        parts.append(
            "\nReturn a Slack-ready Clara response. Include what you checked, findings, verification, and next action."
        )
    else:
        parts.append(
            "\nReturn a Hermes CLI-ready response. Use the same concise boxed-response style expected from hermes-codex, not a Slack report."
        )
    return "\n\n".join(parts)


def _json_from_mixed_stdout(stdout: str) -> dict[str, Any] | None:
    """Parse Claude JSON output even when warnings precede it."""
    text = stdout or ""
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        # Try line-by-line for future stream/noisy variants.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return None


def _format_compact_tokens(count: int) -> str:
    if count >= 1_000_000:
        value = count / 1_000_000
        return f"{value:.1f}M".replace(".0M", "M")
    return f"{round(count / 1000)}K"


def format_token_usage_line(parsed: dict[str, Any] | None) -> str:
    """Render a statusline-style context usage line from Claude CLI result JSON.

    Mirrors the local statusline format, e.g.
    ``⚕ Clara fable-5 │ 102K/272K │ [████░░░░░░] 37%``.
    Returns an empty string when the result JSON lacks usage data.
    """
    if not isinstance(parsed, dict):
        return ""
    model_usage = parsed.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return ""
    model_name, stats = next(iter(model_usage.items()))
    if not isinstance(stats, dict):
        return ""
    try:
        window = int(stats.get("contextWindow") or 0)
    except (TypeError, ValueError):
        window = 0
    usage = parsed.get("usage")
    iterations = usage.get("iterations") if isinstance(usage, dict) else None
    last = iterations[-1] if isinstance(iterations, list) and iterations else None
    used = 0
    if isinstance(last, dict):
        for key in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        ):
            try:
                used += int(last.get(key) or 0)
            except (TypeError, ValueError):
                continue
    if used <= 0 or window <= 0:
        return ""
    short_model = str(model_name).replace("claude-", "")
    # Context pressure is reported once, as the raw token count (e.g. 102K/272K).
    # The redundant bar+% graphic is intentionally omitted so the footer never
    # shows two context indicators that look like conflicting readings.
    return (
        f"⚕ Clara {short_model} │ "
        f"{_format_compact_tokens(used)}/{_format_compact_tokens(window)}"
    )


def _safe_env() -> dict[str, str]:
    env = dict(os.environ)
    # Force Claude Code to use its logged-in account/keychain path rather than
    # accidentally taking a process-level Anthropic API key and billing the API.
    for key in _SECRET_ENV_KEYS:
        env.pop(key, None)
    return env


def _emit_progress(message: str) -> None:
    """Best-effort parent-process progress for long non-streaming Claude jobs."""
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:
        pass


def _run_claude_subprocess(
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: int,
    job_id: str,
    progress_interval: int,
    cancel_event: Any | None = None,
    progress_callback: Any | None = None,
) -> tuple[str, str, int]:
    """Run Claude Code while emitting heartbeat progress from the parent.

    Claude Code's JSON output is only valid at process completion, so Hermes
    cannot true-stream the final assistant text here.  The heartbeat prevents
    CLI/Wave users from seeing a completely silent pane during long Clara turns.
    """
    started = time.time()
    interval = max(0, int(progress_interval or 0))
    next_progress = started + interval if interval else float("inf")
    if progress_callback is not None:
        try:
            progress_callback("bridge.spawned", f"Claude Code CLI 프로세스 시작: job {job_id}", {"job_id": job_id})
        except Exception:
            pass
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    while True:
        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            stderr = (stderr or "") + "\nClaude Code CLI interrupted by user."
            if progress_callback is not None:
                try:
                    progress_callback("bridge.interrupted", "Claude Code CLI 작업을 중단했습니다.", {"job_id": job_id})
                except Exception:
                    pass
            return stdout or "", stderr, 130
        now = time.time()
        remaining = timeout - (now - started)
        if remaining <= 0:
            proc.kill()
            stdout, stderr = proc.communicate()
            stderr = (stderr or "") + f"\nClaude Code CLI timed out after {timeout}s."
            if progress_callback is not None:
                try:
                    progress_callback("bridge.timeout", f"Claude Code CLI가 {timeout}s 제한에 도달했습니다.", {"job_id": job_id})
                except Exception:
                    pass
            return stdout or "", stderr, 124
        wait_for = min(1.0, remaining)
        if interval:
            wait_for = min(wait_for, max(0.0, next_progress - now))
        try:
            stdout, stderr = proc.communicate(timeout=max(0.05, wait_for))
            if progress_callback is not None:
                try:
                    progress_callback(
                        "bridge.completed",
                        f"Claude Code CLI 프로세스 종료: exit {int(proc.returncode or 0)}",
                        {"job_id": job_id, "elapsed_seconds": round(time.time() - started, 1)},
                    )
                except Exception:
                    pass
            return stdout or "", stderr or "", int(proc.returncode or 0)
        except subprocess.TimeoutExpired:
            if interval and time.time() >= next_progress:
                elapsed = int(time.time() - started)
                message = f"Claude Code CLI 실행 중… {elapsed}s elapsed, job {job_id}"
                _emit_progress(f"🟪 Clara/클라라 — {message}")
                if progress_callback is not None:
                    try:
                        progress_callback("heartbeat", message, {"job_id": job_id, "elapsed_seconds": elapsed})
                    except Exception:
                        pass
                next_progress = time.time() + interval


def _session_map_path(hermes_home: Path) -> Path:
    return Path(hermes_home) / _CLAUDE_SESSION_MAP


def _normalize_bridge_session_key(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    # Keep keys filesystem/log friendly while preserving enough uniqueness for
    # Hermes session IDs, Slack thread IDs, and Wave pane labels.
    return re.sub(r"[^A-Za-z0-9_.:@-]+", "-", text)[:160]


def _load_claude_session_map(hermes_home: Path) -> dict[str, Any]:
    path = _session_map_path(hermes_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_claude_session_map(hermes_home: Path, data: dict[str, Any]) -> None:
    path = _session_map_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _remember_claude_session(
    *,
    hermes_home: Path,
    bridge_session_key: str | None,
    claude_session_id: str | None,
    workdir: str,
    job_id: str,
) -> None:
    key = _normalize_bridge_session_key(bridge_session_key)
    sid = str(claude_session_id or "").strip()
    if not key or not sid:
        return
    data = _load_claude_session_map(hermes_home)
    data[key] = {
        "session_id": sid,
        "workdir": workdir,
        "updated_at": time.time(),
        "job_id": job_id,
    }
    _save_claude_session_map(hermes_home, data)


def _lookup_claude_session(
    *,
    hermes_home: Path,
    bridge_session_key: str | None,
    workdir: str,
    enabled: bool,
) -> str | None:
    if not enabled:
        return None
    key = _normalize_bridge_session_key(bridge_session_key)
    if not key:
        return None
    entry = _load_claude_session_map(hermes_home).get(key)
    if not isinstance(entry, dict):
        return None
    # Claude Code sessions are workspace-sensitive.  Avoid resuming a pane's old
    # session if the active workdir changed under the same Hermes session.
    if str(entry.get("workdir") or "") != str(workdir):
        return None
    sid = str(entry.get("session_id") or "").strip()
    return sid or None


def _write_bridge_ledger(
    *,
    hermes_home: Path,
    bridge_session_key: str | None,
    result_text: str,
    exit_code: int,
) -> None:
    """Record this Claude bridge turn to the shared active ledger.

    Best-effort: the native (codex) runtime reads these entries at turn start
    so it knows what Clara just did. Never raises into the bridge.
    """
    try:
        from agent import team_active_ledger

        summary = team_active_ledger.build_turn_summary(
            result_text,
            end_reason="ok" if exit_code == 0 else f"exit:{exit_code}",
        )
        if summary:
            team_active_ledger.write_turn(
                runtime=team_active_ledger.RUNTIME_CLAUDE,
                summary=summary,
                session_id=bridge_session_key,
                end_reason="ok" if exit_code == 0 else f"exit:{exit_code}",
                hermes_home=hermes_home,
            )
    except Exception:
        pass  # ledger is best-effort; never break the bridge turn


def run_claude_code_bridge_sync(
    *,
    config: dict[str, Any] | None,
    message: str,
    context_prompt: str | None,
    channel_prompt: str | None,
    history: Iterable[dict[str, Any]] | None,
    hermes_home: Path,
    bridge_session_key: str | None = None,
    cancel_event: Any | None = None,
    progress_callback: Any | None = None,
) -> ClaudeCodeBridgeResult:
    """Run a gateway turn via local Claude Code CLI and return a Hermes result."""
    bcfg = bridge_config(config)
    claude_bin = str(bcfg.get("command") or shutil.which("claude") or "claude")
    timeout = int(bcfg.get("timeout_seconds") or bcfg.get("timeout") or 1800)
    progress_interval = int(bcfg.get("progress_interval_seconds") or bcfg.get("progress_interval") or 15)
    agent_cfg = config.get("agent") if isinstance(config, dict) else {}
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    # Prefer the bridge-specific cap when configured; otherwise fall back to the
    # normal Hermes agent loop budget.  This keeps clara_cli.max_turns meaningful
    # for speed tuning without breaking existing configs that only set agent.*.
    max_turns = int(bcfg.get("max_turns") or agent_cfg.get("max_turns") or 20)
    configured_allowed_tools = bcfg.get("allowed_tools")
    history_limit = int(bcfg.get("history_limit") or 12)
    model = str(bcfg.get("model") or "").strip()
    effort = str(bcfg.get("effort") or "").strip()
    permission_mode = str(bcfg.get("permission_mode") or "bypassPermissions").strip()
    role_mode = str(bcfg.get("role_mode") or bcfg.get("role") or "").strip()
    if not role_mode:
        try:
            from gateway.orchestrator_modes import read_mode, MODE_CLARA_LEAD
            role_mode = "clara-lead" if read_mode(hermes_home).get("mode") == MODE_CLARA_LEAD else "reviewer"
        except Exception:
            role_mode = "reviewer"
    role_is_lead = role_mode.strip().casefold().replace("_", "-") in {"lead", "clara-lead", "orchestrator", "coder"}
    # In clara-lead mode Clara takes Hugo's lead role, so do not apply the
    # review-mode allowlist unless the profile explicitly configured one.
    # Claude Code still runs under the user's local subscription login and the
    # prompt carries Hugo-equivalent operating instructions.
    if configured_allowed_tools:
        allowed_tools = str(configured_allowed_tools)
    elif role_is_lead:
        allowed_tools = ""
    else:
        allowed_tools = "".join(_DEFAULT_ALLOWED_TOOLS)

    workdir = resolve_workdir(config, message, hermes_home=hermes_home)
    resume_disabled_env = str(os.environ.get(_ENV_DISABLE_RESUME, "")).strip().casefold() in {"1", "true", "yes", "on"}
    resume_cfg = bcfg.get("resume_enabled")
    resume_disabled_config = isinstance(resume_cfg, bool) and not resume_cfg
    resume_enabled = not (resume_disabled_env or resume_disabled_config)
    # One-shot 100% model-swap handoff: on the FIRST turn after /model-swap,
    # force-resume the prior native session under the new model regardless of
    # the steady-state resume_enabled config, and inject the FULL conversation
    # text (history_limit=0) as a backstop. Consumed on success below.
    force_handoff = clara_handoff_pending()
    if force_handoff:
        resume_enabled = True
        history_limit = 0
    resume_session_id = _lookup_claude_session(
        hermes_home=hermes_home,
        bridge_session_key=bridge_session_key,
        workdir=workdir,
        enabled=resume_enabled,
    )
    continuity_context = build_continuity_context(
        hermes_home=hermes_home,
        message=message,
        workdir=workdir,
    )
    job_id = f"clara-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    log_dir = hermes_home / "clara-jobs" / job_id
    log_dir.mkdir(parents=True, exist_ok=True)

    prompt = build_claude_prompt(
        message=message,
        context_prompt=context_prompt,
        channel_prompt=channel_prompt,
        history=history,
        history_limit=history_limit,
        workdir=workdir,
        role_mode=role_mode,
        continuity_context=continuity_context,
    )
    (log_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    metadata = {
        "job_id": job_id,
        "workdir": workdir,
        "created_at": time.time(),
        "provider": "claude-code-cli",
        "max_turns": max_turns,
        "allowed_tools": allowed_tools or "default",
        "timeout_seconds": timeout,
        "progress_interval_seconds": progress_interval,
        "role_mode": role_mode,
        "bridge_session_key": _normalize_bridge_session_key(bridge_session_key),
        "resume_session_id": resume_session_id,
        "resume_enabled": resume_enabled,
        "force_model_swap_handoff": force_handoff,
    }
    (log_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    args = [
        claude_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
    ]
    if resume_session_id:
        args[1:1] = ["--resume", resume_session_id]
    if allowed_tools:
        args.extend(["--allowedTools", allowed_tools])
    if permission_mode:
        args.extend(["--permission-mode", permission_mode])
    if model:
        args.extend(["--model", model])
    if effort:
        args.extend(["--effort", effort])
    if bcfg.get("strict_mcp"):
        # Skip filesystem MCP config (memory/jina/context7) — the reviewer role
        # uses local tools only, and spawning those servers costs ~4s CPU per
        # cold start. With no --mcp-config flags this loads zero MCP servers.
        args.append("--strict-mcp-config")

    try:
        stdout, stderr, exit_code = _run_claude_subprocess(
            args,
            cwd=workdir,
            env=_safe_env(),
            timeout=timeout,
            job_id=job_id,
            progress_interval=progress_interval,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
    except subprocess.TimeoutExpired as exc:
        # Defensive fallback for tests/monkeypatches that still raise the old
        # subprocess.run-style timeout exception.
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        stderr += f"\nClaude Code CLI timed out after {timeout}s."
        exit_code = 124

    (log_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (log_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    parsed = _json_from_mixed_stdout(stdout)
    if parsed is not None:
        (log_dir / "result.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _remember_claude_session(
            hermes_home=hermes_home,
            bridge_session_key=bridge_session_key,
            claude_session_id=parsed.get("session_id"),
            workdir=workdir,
            job_id=job_id,
        )

    if exit_code == 0 and parsed and not parsed.get("is_error"):
        if force_handoff:
            mark_clara_handoff_done()
        result_text = str(parsed.get("result") or "").strip()
        if not result_text:
            result_text = "Claude Code CLI completed but returned an empty result."
    else:
        result_text = _format_failure_result(
            parsed=parsed,
            stderr=stderr,
            stdout=stdout,
            job_id=job_id,
            exit_code=exit_code,
            log_dir=log_dir,
            max_turns=max_turns,
        )

    prefix = str(bcfg.get("response_prefix") or "🟪 Clara/클라라 — ")
    if prefix:
        # The model may emit the marker itself with trailing newline/space
        # variants; strip every leading occurrence, then prepend exactly one.
        marker = prefix.strip()
        body = result_text.lstrip()
        while marker and body.startswith(marker):
            body = body[len(marker):].lstrip()
        result_text = prefix + body
    if bool(bcfg.get("show_job_footer", False)):
        result_text += f"\n\n_Claude Code CLI job: {job_id}_"
    if bool(bcfg.get("show_token_usage_footer", False)):
        usage_line = format_token_usage_line(parsed)
        if usage_line:
            result_text += f"\n_{usage_line}_"

    _write_bridge_ledger(
        hermes_home=hermes_home,
        bridge_session_key=bridge_session_key,
        result_text=result_text,
        exit_code=exit_code,
    )

    return ClaudeCodeBridgeResult(
        final_response=result_text,
        job_id=job_id,
        workdir=workdir,
        log_dir=str(log_dir),
        exit_code=exit_code,
        raw_json=parsed,
    )


def _format_bridge_result_text(
    *,
    parsed: dict[str, Any] | None,
    exit_code: int,
    job_id: str,
    log_dir: Path,
    max_turns: int,
    bcfg: dict[str, Any],
    stderr: str = "",
    stdout: str = "",
) -> str:
    """Shared success/failure -> Slack text rendering for both bridge paths."""
    if exit_code == 0 and parsed and not parsed.get("is_error"):
        result_text = str(parsed.get("result") or "").strip()
        if not result_text:
            result_text = "Claude Code CLI completed but returned an empty result."
    else:
        result_text = _format_failure_result(
            parsed=parsed,
            stderr=stderr,
            stdout=stdout,
            job_id=job_id,
            exit_code=exit_code,
            log_dir=log_dir,
            max_turns=max_turns,
        )
    prefix = str(bcfg.get("response_prefix") or "🟪 Clara/클라라 — ")
    if prefix:
        marker = prefix.strip()
        body = result_text.lstrip()
        while marker and body.startswith(marker):
            body = body[len(marker):].lstrip()
        result_text = prefix + body
    if bool(bcfg.get("show_job_footer", False)):
        result_text += f"\n\n_Claude Code CLI job: {job_id}_"
    if bool(bcfg.get("show_token_usage_footer", False)):
        usage_line = format_token_usage_line(parsed)
        if usage_line:
            result_text += f"\n_{usage_line}_"
    return result_text


def run_claude_code_bridge_resident(
    *,
    config: dict[str, Any] | None,
    message: str,
    context_prompt: str | None,
    channel_prompt: str | None,
    history: Iterable[dict[str, Any]] | None,
    hermes_home: Path,
    bridge_session_key: str | None = None,
    progress_callback: Any | None = None,
) -> ClaudeCodeBridgeResult:
    """Run a gateway turn through a resident (warm) Claude Code CLI process.

    Keeps a long-lived ``claude`` stream-json process per (session, workdir) so
    conversation context survives in process memory, independent of Anthropic's
    ~5 minute prompt-cache TTL.  Any process/protocol failure transparently
    falls back to :func:`run_claude_code_bridge_sync`, so this path can never be
    a single point of failure.
    """
    bcfg = bridge_config(config)
    if not bcfg.get("resident_enabled"):
        return run_claude_code_bridge_sync(
            config=config,
            message=message,
            context_prompt=context_prompt,
            channel_prompt=channel_prompt,
            history=history,
            hermes_home=hermes_home,
            bridge_session_key=bridge_session_key,
            progress_callback=progress_callback,
        )

    claude_bin = str(bcfg.get("command") or shutil.which("claude") or "claude")
    timeout = int(bcfg.get("timeout_seconds") or bcfg.get("timeout") or 1800)
    idle_timeout = float(bcfg.get("resident_idle_timeout_seconds") or 1200)
    max_processes = int(bcfg.get("resident_max_processes") or 8)
    agent_cfg = config.get("agent") if isinstance(config, dict) else {}
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    max_turns = int(bcfg.get("sdk_max_turns") or bcfg.get("max_turns") or agent_cfg.get("max_turns") or 20)
    configured_allowed_tools = bcfg.get("allowed_tools")
    history_limit = int(bcfg.get("history_limit") or 12)
    model = str(bcfg.get("model") or "").strip()
    effort = str(bcfg.get("sdk_effort") or bcfg.get("effort") or "").strip()
    permission_mode = str(bcfg.get("permission_mode") or "bypassPermissions").strip()
    role_mode = str(bcfg.get("role_mode") or bcfg.get("role") or "").strip()
    if not role_mode:
        try:
            from gateway.orchestrator_modes import read_mode, MODE_CLARA_LEAD
            role_mode = "clara-lead" if read_mode(hermes_home).get("mode") == MODE_CLARA_LEAD else "reviewer"
        except Exception:
            role_mode = "reviewer"
    role_is_lead = role_mode.strip().casefold().replace("_", "-") in {"lead", "clara-lead", "orchestrator", "coder"}
    if configured_allowed_tools:
        allowed_tools = str(configured_allowed_tools)
    elif role_is_lead:
        allowed_tools = ""
    else:
        allowed_tools = "".join(_DEFAULT_ALLOWED_TOOLS)

    workdir = resolve_workdir(config, message, hermes_home=hermes_home)
    resume_disabled_env = str(os.environ.get(_ENV_DISABLE_RESUME, "")).strip().casefold() in {"1", "true", "yes", "on"}
    resume_cfg = bcfg.get("resume_enabled")
    resume_disabled_config = isinstance(resume_cfg, bool) and not resume_cfg
    resume_enabled = not (resume_disabled_env or resume_disabled_config)
    # One-shot 100% model-swap handoff (see run_claude_code_bridge_sync): the
    # first turn after /model-swap force-resumes the prior native session under
    # the new model and injects the FULL conversation text as a backstop.
    force_handoff = clara_handoff_pending()
    if force_handoff:
        resume_enabled = True
        history_limit = 0
    resume_session_id = _lookup_claude_session(
        hermes_home=hermes_home,
        bridge_session_key=bridge_session_key,
        workdir=workdir,
        enabled=resume_enabled,
    )
    continuity_context = build_continuity_context(
        hermes_home=hermes_home,
        message=message,
        workdir=workdir,
    )
    job_id = f"clara-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    log_dir = hermes_home / "clara-jobs" / job_id
    log_dir.mkdir(parents=True, exist_ok=True)

    first_prompt = build_claude_prompt(
        message=message,
        context_prompt=context_prompt,
        channel_prompt=channel_prompt,
        history=history,
        history_limit=history_limit,
        workdir=workdir,
        role_mode=role_mode,
        continuity_context=continuity_context,
    )
    (log_dir / "prompt.txt").write_text(first_prompt, encoding="utf-8")

    base_extra_args: list[str] = ["--max-turns", str(max_turns)]
    if allowed_tools:
        base_extra_args.extend(["--allowedTools", allowed_tools])
    if permission_mode:
        base_extra_args.extend(["--permission-mode", permission_mode])
    if model:
        base_extra_args.extend(["--model", model])
    if effort:
        base_extra_args.extend(["--effort", effort])
    if bcfg.get("strict_mcp"):
        # Same as the cold path: drop MCP servers for the resident process so
        # each warm claude doesn't keep memory/jina/context7 attached.
        base_extra_args.append("--strict-mcp-config")

    pool_key = "|".join(
        [
            _normalize_bridge_session_key(bridge_session_key) or "default",
            str(workdir),
        ]
    )

    metadata = {
        "job_id": job_id,
        "workdir": workdir,
        "created_at": time.time(),
        "provider": "claude-code-cli",
        "delivery": "resident",
        "max_turns": max_turns,
        "sdk_max_buffer_size": _as_int(bcfg.get("sdk_max_buffer_size"), 8 * 1024 * 1024),
        "allowed_tools": allowed_tools or "default",
        "timeout_seconds": timeout,
        "role_mode": role_mode,
        "bridge_session_key": _normalize_bridge_session_key(bridge_session_key),
        "resume_session_id": resume_session_id,
        "resume_enabled": resume_enabled,
        "force_model_swap_handoff": force_handoff,
        "resident_idle_timeout_seconds": idle_timeout,
        "resident_max_processes": max_processes,
    }
    (log_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if _as_bool(bcfg.get("sdk_enabled"), False):
        try:
            from gateway.claude_agent_sdk_bridge import run_sdk_turn

            parsed = run_sdk_turn(
                prompt=first_prompt,
                workdir=workdir,
                claude_bin=claude_bin,
                max_turns=max_turns,
                timeout=timeout,
                permission_mode=permission_mode,
                model=model,
                effort=effort,
                allowed_tools=_split_allowed_tools(allowed_tools),
                tools_mode=str(bcfg.get("sdk_tools") or "claude_code"),
                setting_sources=bcfg.get("sdk_setting_sources"),
                include_partial_messages=bool(bcfg.get("sdk_include_partial_messages", True)),
                include_hook_events=bool(bcfg.get("sdk_include_hook_events", False)),
                resume_session_id=resume_session_id,
                max_buffer_size=metadata["sdk_max_buffer_size"],
                strict_mcp_config=_as_bool(bcfg.get("strict_mcp"), False),
                log_dir=log_dir,
                progress_callback=progress_callback,
            )
            parsed["provider"] = "claude-code-cli"
            parsed["delivery"] = "agent-sdk"
            (log_dir / "result.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            _remember_claude_session(
                hermes_home=hermes_home,
                bridge_session_key=bridge_session_key,
                claude_session_id=parsed.get("session_id"),
                workdir=workdir,
                job_id=job_id,
            )
            is_error = bool(parsed.get("is_error")) or str(parsed.get("subtype") or "success") != "success"
            exit_code = 1 if is_error else 0
            if force_handoff and exit_code == 0:
                mark_clara_handoff_done()
            result_text = _format_bridge_result_text(
                parsed=parsed,
                exit_code=exit_code,
                job_id=job_id,
                log_dir=log_dir,
                max_turns=max_turns,
                bcfg=bcfg,
            )
            _write_bridge_ledger(
                hermes_home=hermes_home,
                bridge_session_key=bridge_session_key,
                result_text=result_text,
                exit_code=exit_code,
            )
            return ClaudeCodeBridgeResult(
                final_response=result_text,
                job_id=job_id,
                workdir=workdir,
                log_dir=str(log_dir),
                exit_code=exit_code,
                raw_json=parsed,
            )
        except Exception as sdk_exc:
            error_detail = f"{type(sdk_exc).__name__}: {sdk_exc}"
            if _is_error_max_turns(None, stdout=error_detail):
                error_text = _format_failure_result(
                    parsed=None,
                    stderr="",
                    stdout=error_detail,
                    job_id=job_id,
                    exit_code=1,
                    log_dir=log_dir,
                    max_turns=max_turns,
                )
            else:
                error_text = (
                    "⚠️ Clara Claude Agent SDK 경로가 실패했습니다.\n"
                    "현재 hermes-claude는 Sangkun 지시에 따라 Claude CLI fallback을 자동 실행하지 않습니다.\n"
                    f"오류: {error_detail}\n"
                    f"job_id: {job_id}\n"
                    f"log_dir: {log_dir}\n"
                )
            (log_dir / "sdk-error.log").write_text(error_text, encoding="utf-8")
            if progress_callback is not None:
                try:
                    progress_callback("sdk.error", "Claude Agent SDK 경로가 실패했습니다. CLI fallback은 실행하지 않았습니다.", {"job_id": job_id})
                except Exception:
                    pass
            return ClaudeCodeBridgeResult(
                final_response=error_text,
                job_id=job_id,
                workdir=workdir,
                log_dir=str(log_dir),
                exit_code=1,
                raw_json={"delivery": "agent-sdk", "is_error": True, "error": str(sdk_exc)},
            )

    from gateway.claude_resident import get_pool, ResidentTurnError

    pool = get_pool(idle_timeout=idle_timeout, max_processes=max_processes)
    if progress_callback is not None:
        try:
            progress_callback("resident.started", f"Claude Code resident 런타임 실행: job {job_id}", {"job_id": job_id})
        except Exception:
            pass
    parsed: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            # First attempt may resume the native Claude session for continuity.
            # If Claude Code returns a zero-turn startup/runtime failure, retry
            # once without --resume; this sheds a corrupted/stale native session
            # while preserving the resident bridge as a warm-runtime optimization.
            attempt_extra_args = list(base_extra_args)
            if resume_session_id and attempt == 0:
                attempt_extra_args[:0] = ["--resume", resume_session_id]
            parsed = pool.run_turn(
                key=pool_key,
                workdir=workdir,
                claude_bin=claude_bin,
                extra_args=attempt_extra_args,
                env=_safe_env(),
                first_prompt=first_prompt,
                followup_text=message,
                timeout=timeout,
            )
            if _is_resident_startup_execution_error(parsed):
                errors = parsed.get("errors") if isinstance(parsed.get("errors"), list) else []
                last_error = ResidentTurnError(
                    "resident startup execution error: " + "; ".join(map(str, errors[:3]))
                )
                pool.invalidate(pool_key)
                parsed = None
                continue
            break
        except ResidentTurnError as exc:
            # Drop the (possibly wedged) process and retry once with a fresh
            # spawn; if that also fails, fall back to the classic per-turn path.
            last_error = exc
            pool.invalidate(pool_key)
            continue

    if parsed is None:
        (log_dir / "stderr.log").write_text(
            f"resident path failed, fell back to sync spawn: {last_error}",
            encoding="utf-8",
        )
        return run_claude_code_bridge_sync(
            config=config,
            message=message,
            context_prompt=context_prompt,
            channel_prompt=channel_prompt,
            history=history,
            hermes_home=hermes_home,
            bridge_session_key=bridge_session_key,
            progress_callback=progress_callback,
        )

    (log_dir / "result.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _remember_claude_session(
        hermes_home=hermes_home,
        bridge_session_key=bridge_session_key,
        claude_session_id=parsed.get("session_id"),
        workdir=workdir,
        job_id=job_id,
    )

    is_error = bool(parsed.get("is_error")) or str(parsed.get("subtype") or "success") != "success"
    exit_code = 1 if is_error else 0
    if force_handoff and exit_code == 0:
        mark_clara_handoff_done()
    if progress_callback is not None:
        try:
            progress_callback("resident.completed", f"Claude Code resident 작업 종료: exit {exit_code}", {"job_id": job_id})
        except Exception:
            pass
    result_text = _format_bridge_result_text(
        parsed=parsed,
        exit_code=exit_code,
        job_id=job_id,
        log_dir=log_dir,
        max_turns=max_turns,
        bcfg=bcfg,
    )

    _write_bridge_ledger(
        hermes_home=hermes_home,
        bridge_session_key=bridge_session_key,
        result_text=result_text,
        exit_code=exit_code,
    )

    return ClaudeCodeBridgeResult(
        final_response=result_text,
        job_id=job_id,
        workdir=workdir,
        log_dir=str(log_dir),
        exit_code=exit_code,
        raw_json=parsed,
    )
