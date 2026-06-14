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
