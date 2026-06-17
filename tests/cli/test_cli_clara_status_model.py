import importlib
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load_cli_module():
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs):
        import cli as mod

        return importlib.reload(mod)


def test_clara_status_model_strips_role_prefix_and_formats_opus():
    mod = _load_cli_module()

    assert mod.HermesCLI._format_claude_code_status_model("claude-opus-4-8") == "opus-4.8"
    assert mod.HermesCLI._format_claude_code_status_model("anthropic/claude-opus-4-8") == "opus-4.8"
    assert mod.HermesCLI._format_claude_code_status_model("anthropic/claude-opus-4.8") == "opus-4.8"


def test_clara_status_model_keeps_non_opus_model_without_clara_prefix():
    mod = _load_cli_module()

    assert mod.HermesCLI._format_claude_code_status_model("claude-fable-5") == "fable-5"


def test_clara_lead_display_runtime_uses_sdk_provider(monkeypatch):
    mod = _load_cli_module()
    cli = mod.HermesCLI.__new__(mod.HermesCLI)
    cli.config = {"clara_cli": {"sdk_enabled": True, "model": "claude-opus-4-8"}}
    cli.model = "gpt-5.5"
    cli.provider = "openai-codex"
    monkeypatch.setenv("HERMES_LEAD_MODE", "clara-lead")

    labels = cli._get_display_runtime_labels()

    assert labels == {"model": "opus-4.8", "provider": "claude-code-sdk"}


def test_hugo_display_runtime_keeps_configured_provider(monkeypatch):
    mod = _load_cli_module()
    cli = mod.HermesCLI.__new__(mod.HermesCLI)
    cli.config = {"clara_cli": {"sdk_enabled": True, "model": "claude-opus-4-8"}}
    cli.model = "gpt-5.5"
    cli.provider = "openai-codex"
    monkeypatch.setenv("HERMES_LEAD_MODE", "hugo-lead")

    labels = cli._get_display_runtime_labels()

    assert labels == {"model": "gpt-5.5", "provider": "openai-codex"}
