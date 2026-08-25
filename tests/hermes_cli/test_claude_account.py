"""Pane-local Clara account selection tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli import HermesCLI
from hermes_cli.commands import resolve_command


def _write_account(home: Path, profile: str, email: str) -> Path:
    if profile == "default":
        config_dir = home / ".claude"
        config_path = home / ".claude.json"
        config_dir.mkdir(parents=True)
    else:
        config_dir = home / ".claude-accounts" / profile
        config_dir.mkdir(parents=True)
        config_path = config_dir / ".claude.json"
    config_path.write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}}),
        encoding="utf-8",
    )
    return config_dir


def test_selecting_by_email_local_part_changes_only_supplied_environment(tmp_path):
    from hermes_cli.claude_account import apply_claude_account

    default_dir = _write_account(tmp_path, "default", "392yes@gmail.com")
    acct2_dir = _write_account(tmp_path, "acct2", "workpilotlab@gmail.com")
    current_pane = {
        "CLAUDE_CONFIG_DIR": str(acct2_dir),
        "ANTHROPIC_AUTH_TOKEN": "pane-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "pane-oauth",
        "CLAUDE_API_KEY": "pane-api-key",
    }
    other_pane = dict(current_pane)

    selected = apply_claude_account("392yes", home=tmp_path, environ=current_pane)

    assert selected.profile == "default"
    assert selected.email == "392yes@gmail.com"
    assert selected.config_dir == default_dir
    assert "CLAUDE_CONFIG_DIR" not in current_pane
    assert "ANTHROPIC_AUTH_TOKEN" not in current_pane
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in current_pane
    assert "CLAUDE_API_KEY" not in current_pane
    assert other_pane["CLAUDE_CONFIG_DIR"] == str(acct2_dir)
    assert other_pane["ANTHROPIC_AUTH_TOKEN"] == "pane-secret"
    assert other_pane["CLAUDE_API_KEY"] == "pane-api-key"


def test_selecting_by_email_local_part_resolves_named_profile(tmp_path):
    from hermes_cli.claude_account import apply_claude_account

    _write_account(tmp_path, "default", "392yes@gmail.com")
    acct2_dir = _write_account(tmp_path, "acct2", "workpilotlab@gmail.com")
    environ: dict[str, str] = {}

    selected = apply_claude_account(
        "workpilotlab", home=tmp_path, environ=environ
    )

    assert selected.profile == "acct2"
    assert selected.email == "workpilotlab@gmail.com"
    assert environ["CLAUDE_CONFIG_DIR"] == str(acct2_dir)


@pytest.mark.parametrize("selector", ["acct2", "workpilotlab@gmail.com"])
def test_named_profile_resolves_by_profile_name_or_full_email(tmp_path, selector):
    from hermes_cli.claude_account import apply_claude_account

    acct2_dir = _write_account(tmp_path, "acct2", "workpilotlab@gmail.com")
    environ: dict[str, str] = {}

    selected = apply_claude_account(selector, home=tmp_path, environ=environ)

    assert selected.profile == "acct2"
    assert environ["CLAUDE_CONFIG_DIR"] == str(acct2_dir)


def test_switch_removes_every_known_auth_environment_override(tmp_path):
    from hermes_cli.claude_account import STRIPPED_AUTH_ENV_KEYS, apply_claude_account

    _write_account(tmp_path, "default", "392yes@gmail.com")
    environ = {key: "secret" for key in STRIPPED_AUTH_ENV_KEYS}

    apply_claude_account("default", home=tmp_path, environ=environ)

    assert set(STRIPPED_AUTH_ENV_KEYS).isdisjoint(environ)


def test_unknown_selector_leaves_environment_unchanged(tmp_path):
    from hermes_cli.claude_account import ClaudeAccountSelectionError, apply_claude_account

    acct2_dir = _write_account(tmp_path, "acct2", "workpilotlab@gmail.com")
    environ = {
        "CLAUDE_CONFIG_DIR": str(acct2_dir),
        "ANTHROPIC_API_KEY": "unchanged-secret",
    }
    before = dict(environ)

    with pytest.raises(ClaudeAccountSelectionError, match="Unknown Claude account"):
        apply_claude_account("missing", home=tmp_path, environ=environ)

    assert environ == before


def test_claude_account_command_is_registered_for_cli_only():
    command = resolve_command("claude-account")
    alias = resolve_command("cc-account")

    assert command is not None
    assert command.name == "claude-account"
    assert command.cli_only is True
    assert command.args_hint == "[selector|status]"
    assert alias is command


def test_cli_dispatches_claude_account_command():
    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_resume_sessions = None
    cli._handle_claude_account_command = MagicMock()

    assert cli.process_command("/claude-account 392yes") is True
    cli._handle_claude_account_command.assert_called_once_with(
        "/claude-account 392yes"
    )


def test_handler_switches_current_process_and_evicts_resident_pool(
    tmp_path, monkeypatch, capsys
):
    _write_account(tmp_path, "default", "392yes@gmail.com")
    acct2_dir = _write_account(tmp_path, "acct2", "workpilotlab@gmail.com")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(acct2_dir))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-pane-token")
    pool = MagicMock()
    monkeypatch.setattr("gateway.claude_resident.get_pool", lambda: pool)
    cli = HermesCLI.__new__(HermesCLI)
    cli.conversation_history = [{"role": "user", "content": "keep me"}]
    history_before = list(cli.conversation_history)

    cli._handle_claude_account_command("/claude-account 392yes")

    output = capsys.readouterr().out
    assert "392yes@gmail.com" in output
    assert "current CLI pane only" in output
    assert "CLAUDE_CONFIG_DIR" not in __import__("os").environ
    assert "ANTHROPIC_AUTH_TOKEN" not in __import__("os").environ
    assert cli.conversation_history == history_before
    pool.shutdown_all.assert_called_once_with()


def test_handler_warns_when_resident_pool_cannot_be_evicted(
    tmp_path, monkeypatch, capsys
):
    _write_account(tmp_path, "default", "392yes@gmail.com")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "gateway.claude_resident.get_pool",
        MagicMock(side_effect=RuntimeError("pool unavailable")),
    )
    cli = HermesCLI.__new__(HermesCLI)

    cli._handle_claude_account_command("/claude-account 392yes")

    output = capsys.readouterr().out
    assert "restart this pane once" in output
    assert "다음 Clara 턴부터 적용됩니다" not in output
