"""Process-local Claude Code account selection for Hermes CLI panes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping

CLAUDE_CONFIG_ENV = "CLAUDE_CONFIG_DIR"
STRIPPED_AUTH_ENV_KEYS = (
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CCR_OAUTH_TOKEN_FILE",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
)


class ClaudeAccountSelectionError(ValueError):
    """Raised when a pane-local Claude account selector is invalid or ambiguous."""


@dataclass(frozen=True)
class ClaudeAccount:
    """Safe, non-secret metadata for one local Claude Code account profile."""

    profile: str
    email: str | None
    config_dir: Path
    config_path: Path


def _config_path(config_dir: Path, *, default: bool) -> Path:
    local_config = config_dir / ".config.json"
    if local_config.is_file():
        return local_config
    if default:
        return config_dir.parent / ".claude.json"
    return config_dir / ".claude.json"


def _email_from_config(config_path: Path) -> str | None:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    account = payload.get("oauthAccount") if isinstance(payload, dict) else None
    email = account.get("emailAddress") if isinstance(account, dict) else None
    value = str(email or "").strip()
    return value or None


def list_claude_accounts(*, home: Path | None = None) -> list[ClaudeAccount]:
    """Discover default and isolated ``~/.claude-accounts/*`` profiles."""

    home = Path(home) if home is not None else Path.home()
    default_dir = home / ".claude"
    accounts = [
        ClaudeAccount(
            profile="default",
            email=_email_from_config(_config_path(default_dir, default=True)),
            config_dir=default_dir,
            config_path=_config_path(default_dir, default=True),
        )
    ]

    root = home / ".claude-accounts"
    if not root.is_dir():
        return accounts
    resolved_root = root.resolve()
    for candidate in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        try:
            if candidate.resolve().parent != resolved_root:
                continue
        except OSError:
            continue
        config_path = _config_path(candidate, default=False)
        accounts.append(
            ClaudeAccount(
                profile=candidate.name,
                email=_email_from_config(config_path),
                config_dir=candidate,
                config_path=config_path,
            )
        )
    return accounts


def resolve_claude_account(
    selector: str,
    *,
    home: Path | None = None,
) -> ClaudeAccount:
    """Resolve a profile name, full email, or unique email local-part."""

    needle = str(selector or "").strip().casefold()
    accounts = list_claude_accounts(home=home)
    matches: list[ClaudeAccount] = []
    for account in accounts:
        aliases = {account.profile.casefold()}
        if account.email:
            email = account.email.casefold()
            aliases.add(email)
            aliases.add(email.partition("@")[0])
        if needle in aliases:
            matches.append(account)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        profiles = ", ".join(account.profile for account in matches)
        raise ClaudeAccountSelectionError(
            f"Ambiguous Claude account selector {selector!r}: {profiles}"
        )
    available = ", ".join(
        f"{account.profile} ({account.email or 'email unknown'})"
        for account in accounts
    )
    raise ClaudeAccountSelectionError(
        f"Unknown Claude account {selector!r}. Available: {available}"
    )


def current_claude_account(
    *,
    home: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> ClaudeAccount | None:
    """Return the account selected by one process environment, if recognized."""

    env = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    selected = str(env.get(CLAUDE_CONFIG_ENV) or "").strip()
    if not selected:
        return resolve_claude_account("default", home=home)
    selected_path = Path(selected).expanduser()
    for account in list_claude_accounts(home=home):
        if account.config_dir == selected_path:
            return account
    return None


def apply_claude_account(
    selector: str,
    *,
    home: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> ClaudeAccount:
    """Apply an account to only the supplied process environment."""

    env = os.environ if environ is None else environ
    account = resolve_claude_account(selector, home=home)

    for key in STRIPPED_AUTH_ENV_KEYS:
        env.pop(key, None)
    if account.profile == "default":
        env.pop(CLAUDE_CONFIG_ENV, None)
    else:
        env[CLAUDE_CONFIG_ENV] = str(account.config_dir)
    return account
